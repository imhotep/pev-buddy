"""Fetch SFMTA motorcycle parking from DataSF (Socrata).

Dataset: "Parking Meters" (8vzz-qzz9). Motorcycle spaces are the ones with a
black meter cap (`cap_color = Black`), ~2.9k spaces. One row per space, so we
group them per blockface into ~330 parking sites (half of a site's spaces sit
within 4 m of its centre) and write data/motoparking.geojson.

`active_meter_flag`: M active, T temporarily inactive, U unmetered (meter
removed, space kept), P pay-by-plate; all are spaces a moped can use. L is
"legislated for a future meter" and is left out.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import httpx
import orjson

MOTO_URL = "https://data.sf.gov/resource/8vzz-qzz9.json"

# Well above the motorcycle record count (~3k) so one request gets them all.
_LIMIT = 100_000


def fetch_spaces(url: str = MOTO_URL, timeout: float = 60.0) -> list[dict]:
    """Download every black-cap (motorcycle) meter record."""
    resp = httpx.get(
        url,
        params={"cap_color": "Black", "$limit": _LIMIT},
        timeout=timeout,
        follow_redirects=True,
        # Socrata's CDN rejects requests without a User-Agent.
        headers={"User-Agent": "pev-buddy/1.0 (static map data sync)"},
    )
    resp.raise_for_status()
    return resp.json()


def _street(name: str) -> str:
    """'CAPP ST' -> 'Capp St'; SFMTA pads numbered streets: '04TH AVE' -> '4th Ave'."""
    return " ".join((w.lstrip("0") or w).lower() if w[:1].isdigit() else w.capitalize() for w in name.split())


def to_features(rows: list[dict], bbox: tuple[float, float, float, float]) -> list[dict]:
    """Motorcycle spaces inside `bbox`, grouped per blockface into sites."""
    xmin, ymin, xmax, ymax = bbox
    groups: dict[tuple, list[tuple]] = defaultdict(list)
    for d in rows:
        if d.get("active_meter_flag") == "L":
            continue
        coords = (d.get("shape") or {}).get("coordinates") or []
        if len(coords) != 2:
            continue
        lon, lat = coords
        if not (xmin <= lon <= xmax and ymin <= lat <= ymax):
            continue
        key = (d.get("blockface_id") or f"{lon:.4f},{lat:.4f}", d.get("street_name") or "", d.get("on_offstreet_type") or "")
        groups[key].append((lon, lat, d))

    features: list[dict] = []
    for (blockface, street, onoff), spaces in sorted(groups.items()):
        lon = sum(s[0] for s in spaces) / len(spaces)
        lat = sum(s[1] for s in spaces) / len(spaces)
        # Address of the space nearest the site's centre.
        _, _, near = min(spaces, key=lambda s: (s[0] - lon) ** 2 + (s[1] - lat) ** 2)
        num = (near.get("street_num") or "").strip()
        street_t = _street(street) if street else ""
        metered = sum(1 for s in spaces if s[2].get("active_meter_flag") in ("M", "P"))
        features.append(
            {
                "type": "Feature",
                "id": f"moto-{blockface}-{onoff.lower()}",
                "properties": {
                    "name": street_t or "Motorcycle parking",
                    "address": f"{num} {street_t}".strip() if num and num != "0" else street_t or None,
                    "spaces": len(spaces),
                    # Metered spaces need paying; the rest are free (meter
                    # removed or out of service).
                    "metered": metered,
                    "offstreet": onoff == "OFF",
                },
                "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
            }
        )
    return features


def extract_motoparking(bbox, out_path: Path, url: str = MOTO_URL) -> int:
    """Download + group the SF motorcycle parking and write it to `out_path`."""
    features = to_features(fetch_spaces(url), bbox)
    out_path.write_bytes(orjson.dumps({"type": "FeatureCollection", "features": features}))
    print(f"motoparking: {len(features)} sites -> {out_path}")
    return len(features)
