"""Extract SF Overture Maps slices into compact static files.

Outputs (in the data dir):
  roads.geojson       trimmed road segments (routing-relevant properties only)
  connectors.json     {connector_id: [lon, lat]} — server-side network nodes
  stations.geojson    EV charging stations (place: ev_charging_station)
  places.json         named businesses / POIs for place search
  addresses.parquet   street addresses for geocoding
  manifest.json       release, bbox, counts, timestamp

(BikeLink lockers are a separate static slice, written by bikelink_fetch.)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
from shapely import wkb
from tqdm import tqdm

from . import config, geo

# --- property trimming ------------------------------------------------------

FULL_RANGE = 1.0


def _full_between(between) -> bool:
    """True when a `between` range is null/absent or covers the whole segment."""
    if not between:
        return True
    return between[0] <= 0.0 and between[1] >= 1.0


def _heading_mask(when: dict | None) -> int:
    """1 = forward, 2 = backward, 3 = both."""
    h = (when or {}).get("heading")
    if h == "forward":
        return 1
    if h == "backward":
        return 2
    return 3


def _mode_includes(mode: list | None, want: str) -> bool:
    """True when a rule's mode scope applies to `want`.

    A null/absent mode scope means the rule applies to all modes.
    """
    if mode is None:
        return True
    return want in mode


def effective_max_speed_mph(speed_limits: list | None) -> int | None:
    """Pick the dominant posted max speed (mph) for a segment, else None.

    Mode-scoped rules only count when their mode list includes bicycle —
    e.g. an hgv-only 35 mph limit does not describe how fast we may ride.
    """
    if not speed_limits:
        return None
    best = None  # (coverage, -max_speed, value)
    for rule in speed_limits:
        max_speed = rule.get("max_speed")
        if not max_speed:
            continue
        if not _mode_includes((rule.get("when") or {}).get("mode"), "bicycle"):
            continue
        value = max_speed.get("value")
        unit = max_speed.get("unit", "mph")
        if unit == "km/h":
            value = value * 0.621371
        between = rule.get("between")
        coverage = 0.0 if between is None else max(0.0, min(between[1], 1.0) - max(between[0], 0.0))
        key = (coverage, -value)
        if best is None or key > best[0]:
            best = (key, int(round(value)))
    return best[1] if best else None


@dataclass
class RoadTrim:
    """Per-segment routing decision masks."""

    ow: int = 0  # bicycle: bit1 forward OK, bit2 backward OK (denied rules clear bits)
    ow_moto: int = 0  # motorcycle: same mask (moped vehicle type honors these denials)
    bike_desig: int = 0  # designated bicycle facility per heading
    prohibited: list = None  # [[connector_id, segment_id, heading_mask], ...]
    destinations: list = None  # [[to_segment_id, to_connector_id, label, heading_mask], ...]

    def __post_init__(self):
        self.ow = 3  # both directions OK until a denial says otherwise
        self.ow_moto = 3
        if self.prohibited is None:
            self.prohibited = []
        if self.destinations is None:
            self.destinations = []


def trim_segment(props: dict) -> dict | None:
    """Reduce an Overture road segment to routing-relevant fields."""
    cls = props.get("class")
    if cls is None:
        cls = "unknown"

    t = RoadTrim()
    for rule in props.get("access_restrictions") or []:
        if not _full_between(rule.get("between")):
            continue
        hm = _heading_mask(rule.get("when"))
        access = rule.get("access_type")
        when = rule.get("when") or {}
        bike = _mode_includes(when.get("mode"), "bicycle")
        moto = _mode_includes(when.get("mode"), "motorcycle")
        # A denial scoped to vehicle dimensions (weight, height, length, ...)
        # targets trucks and buses — no PEV comes near those limits. Without
        # this, e.g. a "no vehicles over 3 t" rule closed SF's Cayuga Avenue,
        # a designated bike route, to bicycles in both directions.
        if access == "denied" and when.get("vehicle"):
            continue
        if access == "denied":
            if bike:
                t.ow &= ~hm  # this heading is off limits for bicycles
            if moto:
                t.ow_moto &= ~hm  # ... and for mopeds (motorcycle access mode)
        if bike and when.get("mode") is not None and access == "designated":
            t.bike_desig |= hm  # dedicated/painted bicycle facility on this heading

    for p in props.get("prohibited_transitions") or []:
        seq = p.get("sequence") or []
        if seq:
            first = seq[0]
            t.prohibited.append([first.get("connector_id"), first.get("segment_id"), _heading_mask(p.get("when"))])

    for d in props.get("destinations") or []:
        labels = d.get("labels") or []
        if not labels:
            continue
        t.destinations.append(
            [
                d.get("to_segment_id"),
                d.get("to_connector_id"),
                labels[0].get("value"),
                _heading_mask(d.get("when")),
            ]
        )

    conns = sorted(props.get("connectors") or [], key=lambda c: c.get("at") or 0.0)
    if len(conns) < 2:
        return None

    names = props.get("names") or {}
    return {
        "id": props.get("id"),
        "name": names.get("primary"),
        "class": cls,
        "speed": effective_max_speed_mph(props.get("speed_limits")),
        "ow": t.ow,
        "ow_moto": t.ow_moto,
        "bike_desig": t.bike_desig,
        "prohib": t.prohibited,
        "dest": t.destinations,
        "conns": [[c.get("connector_id"), c.get("at")] for c in conns],
    }


# --- extractors -------------------------------------------------------------

def _round_coords(coords) -> list:
    return [[round(lon, 5), round(lat, 5)] for lon, lat in coords]


def extract_roads(bbox, release: str, out_path: Path) -> int:
    import overturemaps as om

    features = []
    reader = om.record_batch_reader("segment", bbox=bbox, release=release)
    for batch in tqdm(reader, desc="segments"):
        for row in batch.to_pylist():
            if row.get("subtype") != "road":
                continue
            trimmed = trim_segment(row)
            if trimmed is None:
                continue
            geometry = row.get("geometry")
            if geometry is None:
                continue
            try:
                shape = wkb.loads(geometry)
            except Exception:
                continue
            if shape.geom_type != "LineString":
                continue
            coords = _round_coords(list(shape.coords))
            features.append(
                {
                    "type": "Feature",
                    "id": trimmed["id"],
                    "properties": trimmed,
                    "geometry": {"type": "LineString", "coordinates": coords},
                }
            )
    out_path.write_bytes(orjson.dumps({"type": "FeatureCollection", "features": features}))
    print(f"roads: {len(features)} features -> {out_path}")
    return len(features)


def extract_connectors(bbox, release: str, out_path: Path) -> int:
    import overturemaps as om

    pts = {}
    reader = om.record_batch_reader("connector", bbox=bbox, release=release)
    for batch in tqdm(reader, desc="connectors"):
        for row in batch.to_pylist():
            cid = row.get("id")
            g = row.get("geometry")
            if cid and g is not None:
                try:
                    shape = wkb.loads(g)
                except Exception:
                    continue
                if shape.geom_type != "Point":
                    continue
                pts[cid] = [round(shape.x, 5), round(shape.y, 5)]
    out_path.write_bytes(orjson.dumps(pts))
    print(f"connectors: {len(pts)} -> {out_path}")
    return len(pts)


def extract_stations(bbox, release: str, out_path: Path) -> int:
    import overturemaps as om

    features = []
    reader = om.record_batch_reader("place", bbox=bbox, release=release)
    for batch in tqdm(reader, desc="places"):
        for row in batch.to_pylist():
            if row.get("basic_category") != "ev_charging_station":
                continue
            g = row.get("geometry")
            if g is None:
                continue
            try:
                shape = wkb.loads(g)
            except Exception:
                continue
            if shape.geom_type != "Point":
                continue
            names = row.get("names") or {}
            brand = (row.get("brand") or {}).get("names") or {}
            addresses = row.get("addresses") or []
            addr = addresses[0] if addresses else {}
            phones = row.get("phones") or []
            websites = row.get("websites") or []
            features.append(
                {
                    "type": "Feature",
                    "id": row.get("id"),
                    "properties": {
                        "name": names.get("primary"),
                        "brand": brand.get("primary"),
                        "address": addr.get("freeform"),
                        "phone": phones[0] if phones else None,
                        "website": websites[0] if websites else None,
                        "confidence": row.get("confidence"),
                    },
                    "geometry": {"type": "Point", "coordinates": [round(shape.x, 5), round(shape.y, 5)]},
                }
            )
    out_path.write_bytes(orjson.dumps({"type": "FeatureCollection", "features": features}))
    print(f"stations: {len(features)} -> {out_path}")
    return len(features)


def extract_places(bbox, release: str, out_path: Path) -> int:
    """Named places (businesses, parks, landmarks, ...) for POI search.

    Written as a compact list of [id, name, category, address, lon, lat].
    Charging stations are excluded (they live in stations.geojson).
    """
    import overturemaps as om

    rows: list = []
    seen: set = set()
    reader = om.record_batch_reader("place", bbox=bbox, release=release)
    for batch in tqdm(reader, desc="places"):
        for row in batch.to_pylist():
            if row.get("basic_category") == "ev_charging_station":
                continue
            names = row.get("names") or {}
            p = names.get("primary")
            name = p.get("text") if isinstance(p, dict) else p
            if not name:
                continue
            g = row.get("geometry")
            if g is None:
                continue
            try:
                shape = wkb.loads(g)
            except Exception:
                continue
            if shape.geom_type != "Point":
                continue
            lon, lat = round(shape.x, 5), round(shape.y, 5)
            key = (name.lower(), lon, lat)
            if key in seen:
                continue
            seen.add(key)
            addresses = row.get("addresses") or []
            addr = addresses[0].get("freeform") if addresses else None
            rows.append([row.get("id"), name, row.get("basic_category"), addr, lon, lat])
    out_path.write_bytes(orjson.dumps(rows))
    print(f"places: {len(rows)} records -> {out_path}")
    return len(rows)


def extract_addresses(bbox, release: str, out_path: Path) -> int:
    import overturemaps as om

    streets, numbers, units, postcodes, lons, lats = [], [], [], [], [], []
    reader = om.record_batch_reader("address", bbox=bbox, release=release)
    for batch in tqdm(reader, desc="addresses"):
        for row in batch.to_pylist():
            g = row.get("geometry")
            street = row.get("street")
            if g is None or not street:
                continue
            try:
                shape = wkb.loads(g)
            except Exception:
                continue
            if shape.geom_type != "Point":
                continue
            streets.append(str(street).upper())
            numbers.append(row.get("number"))
            units.append(row.get("unit"))
            postcodes.append(row.get("postcode"))
            lons.append(round(shape.x, 6))
            lats.append(round(shape.y, 6))
    table = pa.table(
        {
            "street": pa.array(streets),
            "number": pa.array(numbers),
            "unit": pa.array(units),
            "postcode": pa.array(postcodes),
            "lon": pa.array(lons),
            "lat": pa.array(lats),
        }
    )
    pq.write_table(table, out_path)
    print(f"addresses: {len(streets)} -> {out_path}")
    return len(streets)


def sync_all(bbox=None, release: str | None = None, out_dir: Path | None = None, skip: set[str] | None = None) -> dict:
    """Download the SF Overture slices and write the static data files."""
    bbox = tuple(bbox) if bbox else config.SF_BBOX
    release = release or config.OVERTURE_RELEASE
    out_dir = Path(out_dir) if out_dir else config.DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    counts = {}
    t0 = time.time()
    if "roads" not in (skip or set()):
        counts["roads"] = extract_roads(bbox, release, out_dir / "roads.geojson")
    if "connectors" not in (skip or set()):
        counts["connectors"] = extract_connectors(bbox, release, out_dir / "connectors.json")
    if "stations" not in (skip or set()):
        counts["stations"] = extract_stations(bbox, release, out_dir / "stations.geojson")
    if "addresses" not in (skip or set()):
        counts["addresses"] = extract_addresses(bbox, release, out_dir / "addresses.parquet")
    if "places" not in (skip or set()):
        counts["places"] = extract_places(bbox, release, out_dir / "places.json")

    manifest = {
        "release": release,
        "bbox": list(bbox),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": counts,
        "travel_mode": config.TRAVEL_MODE,
        "vehicles": list(config.VEHICLE_ORDER),
        "default_vehicle": config.DEFAULT_VEHICLE,
        "max_routable_speed_mph": config.MAX_SPEED_MPH,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"done in {time.time() - t0:.1f}s; manifest -> {out_dir / 'manifest.json'}")
    return manifest
