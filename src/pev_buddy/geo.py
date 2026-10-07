"""Geographic helpers: distances, bearings, interpolation."""

from __future__ import annotations

import math

import numpy as np

EARTH_RADIUS_M = 6_371_000.0


def haversine(a_lon: float, a_lat: float, b_lon: float, b_lat: float) -> float:
    """Great-circle distance in meters between two WGS84 points."""
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def fast_distances(a_lon: float, a_lat: float, pts: np.ndarray) -> np.ndarray:
    """Approximate distances (meters) from (a_lon, a_lat) to an (N, 2) lon/lat array.

    Equirectangular projection with a single reference latitude (the source
    point's), so it is consistent over city scale — accurate enough for
    snapping. (Scaling each point's longitude by its own latitude would drift
    tens of meters across the city.)
    """
    ref = math.cos(math.radians(a_lat)) * EARTH_RADIUS_M
    x0 = math.radians(a_lon) * ref
    y0 = math.radians(a_lat) * EARTH_RADIUS_M
    x1 = np.radians(pts[:, 0]) * ref
    y1 = np.radians(pts[:, 1]) * EARTH_RADIUS_M
    return np.hypot(x1 - x0, y1 - y0)


def bearing(a_lon: float, a_lat: float, b_lon: float, b_lat: float) -> float:
    """Initial bearing from a to b in degrees [0, 360)."""
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dl = math.radians(b_lon - a_lon)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360.0


def turn_angle(prev_bearing: float, next_bearing: float) -> float:
    """Signed turn angle in degrees: -180 (full left) .. 0 (straight) .. 180 (full right)."""
    d = (next_bearing - prev_bearing + 540.0) % 360.0 - 180.0
    return d


CARDINALS = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]


def cardinal(bearing_deg: float) -> str:
    """Human cardinal direction for a bearing (rounded to the nearest of 8)."""
    return CARDINALS[int((bearing_deg % 360.0) / 45.0 + 0.5) % 8]


def point_on_linestring(coords: list[tuple[float, float]], t: float) -> tuple[float, float]:
    """Interpolate a point at fraction t in [0, 1] along a coordinate list."""
    if t <= 0:
        return coords[0]
    if t >= 1:
        return coords[-1]
    segs = [haversine(*coords[i], *coords[i + 1]) for i in range(len(coords) - 1)]
    total = sum(segs)
    if total == 0:
        return coords[-1]
    target = t * total
    acc = 0.0
    for seg_len, (a, b) in zip(segs, zip(coords, coords[1:])):
        if acc + seg_len >= target or b is coords[-1]:
            f = 0.0 if seg_len == 0 else (target - acc) / seg_len
            return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
        acc += seg_len
    return coords[-1]


def linestring_length_m(coords: list[tuple[float, float]]) -> float:
    """Total length in meters of a coordinate list."""
    return sum(haversine(*coords[i], *coords[i + 1]) for i in range(len(coords) - 1))


M_PER_MILE = 1609.344
FT_PER_M = 3.28084


def format_distance(meters: float) -> str:
    """Imperial display distance, matching the web UI: feet (to the nearest
    10) under a tenth of a mile, miles to one decimal above it — so a short
    step never reads "0.0 mi"."""
    if meters < 0.1 * M_PER_MILE:
        return f"{round(meters * FT_PER_M / 10) * 10:.0f} ft"
    return f"{meters / M_PER_MILE:.1f} mi"
