"""Charging station access (Overture places: ev_charging_station)."""

from __future__ import annotations

import orjson
from pathlib import Path

from . import config


class StationStore:
    def __init__(self, path: Path | None = None):
        path = path or config.DATA_DIR / "stations.geojson"
        with open(path, "rb") as f:
            data = orjson.loads(f.read())
        self.features = data.get("features", [])
        self.stations = []
        for f in self.features:
            p = f["properties"]
            self.stations.append(
                {
                    "id": f.get("id"),
                    "name": p.get("name"),
                    "brand": p.get("brand"),
                    "address": p.get("address"),
                    "phone": p.get("phone"),
                    "website": p.get("website"),
                    "confidence": p.get("confidence"),
                    "lon": f["geometry"]["coordinates"][0],
                    "lat": f["geometry"]["coordinates"][1],
                }
            )

    def __len__(self) -> int:
        return len(self.stations)

    def all(self) -> list[dict]:
        return self.stations

    def nearest(self, lon: float, lat: float, limit: int = 5) -> list[dict]:
        import math

        ranked = sorted(
            self.stations,
            key=lambda s: math.hypot((s["lon"] - lon) * math.cos(math.radians(lat)), s["lat"] - lat),
        )
        out = []
        for s in ranked[:limit]:
            item = dict(s)
            item["distance_m"] = _haversine(lon, lat, s["lon"], s["lat"])
            out.append(item)
        return out

    def as_geojson(self) -> bytes:
        return orjson.dumps({"type": "FeatureCollection", "features": self.features})


def _haversine(a_lon, a_lat, b_lon, b_lat) -> float:
    import math

    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(h))
