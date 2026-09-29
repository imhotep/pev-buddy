"""Fetch SF BikeLink (bikelink.org) locker locations.

BikeLink's /maps page server-renders its national location list into a
data attribute (`data-maps--map-locations-value`, HTML-escaped JSON). We pull
that payload, keep the parking facilities inside our SF bbox, and write a
compact GeoJSON (data/bikelink.geojson). Card-vendor locations are skipped —
they sell access cards, they don't hold a bike.
"""

from __future__ import annotations

import html as html_mod
import re
from pathlib import Path

import httpx
import orjson

BIKELINK_MAPS_URL = "https://www.bikelink.org/maps"

# Facility types that actually store a bike/PEV (everything else, e.g. card
# vendors, is not parking).
PARKING_TYPES = frozenset({"eLocker", "eRack", "Group Parking", "Bike Hangar"})

_LOCATIONS_ATTR = re.compile(r'data-maps--map-locations-value="([^"]+)"')


def fetch_locations(url: str = BIKELINK_MAPS_URL, timeout: float = 30.0) -> list[dict]:
    """Download the national location list from bikelink.org."""
    resp = httpx.get(
        url,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "pev-buddy/1.0 (static map data sync)"},
    )
    resp.raise_for_status()
    m = _LOCATIONS_ATTR.search(resp.text)
    if not m:
        raise RuntimeError("could not find the locations payload in bikelink.org/maps")
    return orjson.loads(html_mod.unescape(m.group(1)))


def to_features(locations: list[dict], bbox: tuple[float, float, float, float]) -> list[dict]:
    """BikeLink locations inside `bbox` (xmin, ymin, xmax, ymax) as GeoJSON features."""
    xmin, ymin, xmax, ymax = bbox
    features: list[dict] = []
    for d in locations:
        if d.get("coming_soon"):
            continue
        ftype = d.get("location_friendly_type")
        if ftype not in PARKING_TYPES:
            continue
        lon, lat = d.get("longitude"), d.get("latitude")
        if lon is None or lat is None or not (xmin <= lon <= xmax and ymin <= lat <= ymax):
            continue
        address = (d.get("street_address") or "").strip()
        features.append(
            {
                "type": "Feature",
                "id": f"bl-{d.get('url_id')}",
                "properties": {
                    "name": (d.get("human_name") or "").strip(),
                    "facility_type": ftype,
                    "address": address or None,
                    "city": (d.get("city") or "").strip() or None,
                    "num_spaces": d.get("num_spaces"),
                    "access_devices": [dev.get("name") for dev in d.get("access_device_types") or []],
                },
                "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
            }
        )
    return features


def extract_bikelink(bbox, out_path: Path, url: str = BIKELINK_MAPS_URL) -> int:
    """Download + trim the SF BikeLink slice and write it to `out_path`."""
    features = to_features(fetch_locations(url), bbox)
    out_path.write_bytes(orjson.dumps({"type": "FeatureCollection", "features": features}))
    print(f"bikelink: {len(features)} features -> {out_path}")
    return len(features)
