"""SFMTA's bikeway network, matched onto the Overture road segments.

Overture tags few of San Francisco's bike lanes (~47 km on-street), and its
"designated for bicycles" flag also marks signed bike routes with no lane at
all (Cayuga Ave, #50). SFMTA publishes the city's own network, every line
with its Caltrans class:

    Class I    bike path, off the street            -> code 1
    Class II   painted bike lane                    -> code 2
    Class III  signed route / sharrows, with cars   -> code 3
    Class IV   separated (protected) bikeway        -> code 4

`fetch` downloads it (ArcGIS layer MTA.bikewaynetwork, the line layer behind
sfmta.com/BikeMap; the same data DataSF publishes) and writes
data/bikeways.geojson. `match` gives each road segment the class SFMTA has
on it, per direction of travel: graph.build turns that into the segment's
cost category (graph._edge_category).
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

import httpx
import orjson

LAYER_URL = "https://services.sfmta.com/arcgis/rest/services/Bicycle/bicycle/MapServer/0/query"
_FIELDS = "STREETNAME,FACILITY_T,SYMBOLOGY,DIRECT,DIR,CONTRAFLOW,LENGTH_FT"
_PAGE = 2000

CLASS_CODES = {"CLASS I": 1, "CLASS II": 2, "CLASS III": 3, "CLASS IV": 4}
# When two lines claim one stretch, the safer facility wins.
_RANK = {0: 0, 3: 1, 2: 2, 1: 3, 4: 4}

MATCH_M = 12.0  # an SFMTA line this close to a segment's centerline is on it
MATCH_DEG = 30.0  # ...and runs within this many degrees of it
# Which SFMTA classes may land on which Overture classes: a lane is on a
# street; a path is a path. Sidewalks only take Class I (a promenade the city
# signs for bikes), never the lane in the street beside them.
_PATHLIKE = frozenset({"cycleway", "path", "track", "pedestrian", "footway"})
_ON_PATHS = frozenset({1, 4})
_ON_FOOTWAY = frozenset({1})
_ON_STREETS = frozenset({2, 3, 4})


def fetch(out_path: Path, url: str = LAYER_URL, timeout: float = 60.0) -> int:
    """Download every bikeway line and write a slim GeoJSON; returns the count."""
    features: list[dict] = []
    offset = 0
    with httpx.Client(timeout=timeout, headers={"User-Agent": "pev-buddy/1.0 (static map data sync)"}) as client:
        while True:
            resp = client.get(
                url,
                params={
                    "where": "1=1",
                    "outFields": _FIELDS,
                    "outSR": 4326,
                    "f": "geojson",
                    "resultOffset": offset,
                    "resultRecordCount": _PAGE,
                },
            )
            resp.raise_for_status()
            page = resp.json()
            rows = page.get("features") or []
            features += [f for f in (to_feature(r) for r in rows) if f]
            if len(rows) < _PAGE and not page.get("exceededTransferLimit"):
                break
            offset += len(rows)
    out_path.write_bytes(orjson.dumps({"type": "FeatureCollection", "features": features}))
    return len(features)


def to_feature(row: dict) -> dict | None:
    """One SFMTA record as {class code, street, direction} + its line."""
    p = row.get("properties") or {}
    geom = row.get("geometry") or {}
    code = CLASS_CODES.get((p.get("FACILITY_T") or "").strip().upper())
    if not code or geom.get("type") != "LineString" or len(geom.get("coordinates") or []) < 2:
        return None
    direction = (p.get("DIR") or "").strip().upper()
    return {
        "type": "Feature",
        "properties": {
            "cls": code,
            "street": (p.get("STREETNAME") or "").strip() or None,
            # The direction of travel the facility serves, when it serves one.
            "dir": direction if direction in ("N", "S", "E", "W") else None,
        },
        "geometry": {"type": "LineString", "coordinates": [[round(x, 6), round(y, 6)] for x, y, *_ in geom["coordinates"]]},
    }


def load(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    return orjson.loads(path.read_bytes())["features"]


# ---------------------------------------------------------------- matching

_CELL = 0.0005  # ~50 m grid cells for the line pieces


def _xy(lon: float, lat: float, kx: float) -> tuple[float, float]:
    return lon * kx, lat * 110_574.0


def _bearing(ax: float, ay: float, bx: float, by: float) -> float:
    return math.degrees(math.atan2(bx - ax, by - ay)) % 360.0


_DIR_DEG = {"N": 0.0, "E": 90.0, "S": 180.0, "W": 270.0}


def _angle(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


class _Index:
    """SFMTA line pieces in a coarse grid: (ax, ay, bx, by, bearing, cls, dir)."""

    def __init__(self, features: list[dict], kx: float):
        self.kx = kx
        self.cells: dict[tuple[int, int], list[tuple]] = defaultdict(list)
        for f in features:
            cls, d = f["properties"]["cls"], f["properties"]["dir"]
            cs = f["geometry"]["coordinates"]
            for (alon, alat), (blon, blat) in zip(cs, cs[1:]):
                ax, ay = _xy(alon, alat, kx)
                bx, by = _xy(blon, blat, kx)
                if ax == bx and ay == by:
                    continue
                piece = (ax, ay, bx, by, _bearing(ax, ay, bx, by), cls, d)
                for cx in range(int(min(alon, blon) // _CELL), int(max(alon, blon) // _CELL) + 1):
                    for cy in range(int(min(alat, blat) // _CELL), int(max(alat, blat) // _CELL) + 1):
                        self.cells[(cx, cy)].append(piece)

    def near(self, lon: float, lat: float, heading: float, allowed: frozenset) -> list[tuple[int, str | None]]:
        """(cls, dir) of the pieces within MATCH_M of the point, along `heading`."""
        px, py = _xy(lon, lat, self.kx)
        cx, cy = int(lon // _CELL), int(lat // _CELL)
        out = []
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for ax, ay, bx, by, brg, cls, d in self.cells.get((gx, gy), ()):
                    if cls not in allowed or _angle(brg % 180.0, heading % 180.0) > MATCH_DEG:
                        continue
                    dx, dy = bx - ax, by - ay
                    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
                    if math.hypot(px - ax - t * dx, py - ay - t * dy) <= MATCH_M:
                        out.append((cls, d))
        return out


def match(roads_features: list[dict], bikeways: list[dict]) -> dict[str, tuple[int, int]]:
    """Per road segment id, the SFMTA class on it as (forward, backward)
    codes; 0 where SFMTA has none. Segments with none either way are left out.

    A segment takes a class when at least two of three points along it (the
    middle one, on short segments) sit on an SFMTA line of that class running
    the same way. A line marked with a direction of travel ("DIR") counts
    only for the heading within 90 degrees of it.
    """
    if not bikeways:
        return {}
    kx = math.cos(math.radians(bikeways[0]["geometry"]["coordinates"][0][1])) * 111_320.0
    index = _Index(bikeways, kx)
    out: dict[str, tuple[int, int]] = {}
    for f in roads_features:
        p = f["properties"]
        cls = p.get("class") or ""
        allowed = _ON_FOOTWAY if cls == "footway" else _ON_PATHS if cls in _PATHLIKE else _ON_STREETS
        coords = f["geometry"]["coordinates"]
        samples = _samples(coords, kx)
        if not samples:
            continue
        need = 2 if len(samples) == 3 else 1
        codes = []
        for heading_off in (0.0, 180.0):  # forward, then backward along the segment
            votes: dict[int, int] = defaultdict(int)
            for lon, lat, brg in samples:
                travel = (brg + heading_off) % 360.0
                seen = set()
                for c, d in index.near(lon, lat, brg, allowed):
                    if d is not None and _angle(_DIR_DEG[d], travel) >= 90.0:
                        continue  # a one-direction facility, serving the other way
                    seen.add(c)
                for c in seen:
                    votes[c] += 1
            won = [c for c, n in votes.items() if n >= need]
            codes.append(max(won, key=_RANK.__getitem__) if won else 0)
        if codes[0] or codes[1]:
            out[p.get("id")] = (codes[0], codes[1])
    return out


def _samples(coords: list, kx: float) -> list[tuple[float, float, float]]:
    """(lon, lat, bearing) at a quarter, half and three quarters of the way
    along a polyline (just the half for one under 30 m)."""
    pts = [_xy(lon, lat, kx) for lon, lat, *_ in coords]
    seg = [math.hypot(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(pts, pts[1:])]
    total = sum(seg)
    if total <= 0:
        return []
    out = []
    for f in (0.25, 0.5, 0.75) if total >= 30.0 else (0.5,):
        want, run = f * total, 0.0
        for i, L in enumerate(seg):
            if L > 0 and run + L >= want:
                t = (want - run) / L
                (ax, ay), (bx, by) = pts[i], pts[i + 1]
                lon = coords[i][0] + (coords[i + 1][0] - coords[i][0]) * t
                lat = coords[i][1] + (coords[i + 1][1] - coords[i][1]) * t
                out.append((lon, lat, _bearing(ax, ay, bx, by)))
                break
            run += L
    return out
