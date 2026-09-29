"""Fetch SFMTA bicycle parking racks from DataSF (Socrata).

Dataset: "Bicycle Parking Racks" (hn4j-6fx5) — every rack installed by the
SFMTA, mostly on sidewalks, plus on-street corrals. ~6k point records with
rack/space counts, placement type, and install year. One SODA call pulls the
whole city; we keep the fields the map needs and write data/racks.geojson.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import orjson

RACKS_URL = "https://data.sf.gov/resource/hn4j-6fx5.json"

# Well above the record count (~6k) so a single request gets the whole city.
_LIMIT = 100_000


def fetch_racks(url: str = RACKS_URL, timeout: float = 60.0) -> list[dict]:
    """Download every rack record from the Socrata SODA API."""
    resp = httpx.get(
        url,
        params={"$limit": _LIMIT},
        timeout=timeout,
        follow_redirects=True,
        # Socrata's CDN rejects requests without a User-Agent.
        headers={"User-Agent": "pev-buddy/1.0 (static map data sync)"},
    )
    resp.raise_for_status()
    return resp.json()


def to_features(rows: list[dict], bbox: tuple[float, float, float, float]) -> list[dict]:
    """Rack records inside `bbox` (xmin, ymin, xmax, ymax) as GeoJSON features."""
    xmin, ymin, xmax, ymax = bbox
    features: list[dict] = []
    for d in rows:
        shape = d.get("shape") or {}
        coords = shape.get("coordinates") or []
        if len(coords) != 2:
            continue
        lon, lat = coords
        if not (xmin <= lon <= xmax and ymin <= lat <= ymax):
            continue
        address = (d.get("address") or "").strip()
        landmark = (d.get("location") or "").strip()
        street = (d.get("street") or "").strip()
        features.append(
            {
                "type": "Feature",
                "id": f"rack-{d.get('objectid')}",
                "properties": {
                    # Best human label: the address, else the street name.
                    "name": address or (f"{street.title()} Street" if street else "Bike rack"),
                    "landmark": landmark or None,
                    "placement": (d.get("placement") or "").title() or None,
                    "racks": int(d["racks"]) if str(d.get("racks") or "").isdigit() else None,
                    "spaces": int(d["spaces"]) if str(d.get("spaces") or "").isdigit() else None,
                    "install_yr": int(d["install_yr"]) if str(d.get("install_yr") or "").isdigit() else None,
                },
                "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
            }
        )
    return features


def extract_racks(bbox, out_path: Path, url: str = RACKS_URL) -> int:
    """Download + trim the SF rack slice and write it to `out_path`."""
    features = to_features(fetch_racks(url), bbox)
    out_path.write_bytes(orjson.dumps({"type": "FeatureCollection", "features": features}))
    print(f"racks: {len(features)} features -> {out_path}")
    return len(features)
