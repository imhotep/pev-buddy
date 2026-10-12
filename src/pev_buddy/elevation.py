"""Ground elevation for the road graph, from the open Terrain Tiles dataset.

Terrain Tiles (AWS Open Data, https://registry.opendata.aws/terrain-tiles/)
serve elevation as "terrarium" PNGs in web-mercator tiles; over the US they
are built from USGS 3DEP bare-earth data. At zoom 14 a pixel is ~7.5 m at
SF's latitude, finer than a city block. The sync step fetches the tiles over
the bbox once and stores the mosaic as elevation.npz; the graph build samples
it at every junction.
"""

from __future__ import annotations

import io
import math
import urllib.request
from pathlib import Path

import numpy as np

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
ZOOM = 14
TILE_PX = 256


def _tile_xy(lon: float, lat: float, z: int) -> tuple[float, float]:
    """Fractional web-mercator tile coordinates of a point."""
    n = 2**z
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def decode_terrarium(rgb: np.ndarray) -> np.ndarray:
    """Terrarium PNG pixels (H, W, 3) -> meters: R*256 + G + B/256 - 32768."""
    rgb = rgb.astype(np.float64)
    return (rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0).astype(np.float32)


def fetch_grid(bbox: tuple[float, float, float, float], z: int = ZOOM, fetch=None) -> dict:
    """Download and mosaic the terrain tiles covering bbox (xmin, ymin, xmax, ymax).

    `fetch(url) -> bytes` is injectable for tests.
    """
    from PIL import Image

    fetch = fetch or (lambda url: urllib.request.urlopen(url, timeout=60).read())
    xmin, ymin, xmax, ymax = bbox
    x0, y0 = (int(v) for v in _tile_xy(xmin, ymax, z))  # north-west tile
    x1, y1 = (int(v) for v in _tile_xy(xmax, ymin, z))  # south-east tile
    grid = np.zeros(((y1 - y0 + 1) * TILE_PX, (x1 - x0 + 1) * TILE_PX), dtype=np.float32)
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            png = fetch(TILE_URL.format(z=z, x=tx, y=ty))
            rgb = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
            r, c = (ty - y0) * TILE_PX, (tx - x0) * TILE_PX
            grid[r : r + TILE_PX, c : c + TILE_PX] = decode_terrarium(rgb)
    return {"grid": grid, "z": z, "x0": x0, "y0": y0}


def save_grid(grid: dict, path: Path) -> None:
    np.savez_compressed(path, grid=grid["grid"], z=grid["z"], x0=grid["x0"], y0=grid["y0"])


def load_grid(path: Path) -> dict | None:
    if not Path(path).exists():
        return None
    d = np.load(path)
    return {"grid": d["grid"], "z": int(d["z"]), "x0": int(d["x0"]), "y0": int(d["y0"])}


def sample(grid: dict, lonlat: np.ndarray) -> np.ndarray:
    """Bilinear elevation (m) at each [lon, lat]; NaN outside the mosaic."""
    pts = np.asarray(lonlat, dtype=np.float64).reshape(-1, 2)
    z, g = grid["z"], grid["grid"]
    n = 2**z
    lat_r = np.radians(pts[:, 1])
    tx = (pts[:, 0] + 180.0) / 360.0 * n
    ty = (1.0 - np.log(np.tan(lat_r) + 1.0 / np.cos(lat_r)) / np.pi) / 2.0 * n
    # Pixel centers sit at +0.5: shift so integer coordinates hit centers.
    px = (tx - grid["x0"]) * TILE_PX - 0.5
    py = (ty - grid["y0"]) * TILE_PX - 0.5
    h, w = g.shape
    inside = (px >= 0) & (py >= 0) & (px <= w - 1) & (py <= h - 1)
    px, py = np.clip(px, 0, w - 1), np.clip(py, 0, h - 1)
    c0, r0 = np.floor(px).astype(int), np.floor(py).astype(int)
    c1, r1 = np.minimum(c0 + 1, w - 1), np.minimum(r0 + 1, h - 1)
    fx, fy = px - c0, py - r0
    top = g[r0, c0] * (1 - fx) + g[r0, c1] * fx
    bot = g[r1, c0] * (1 - fx) + g[r1, c1] * fx
    out = (top * (1 - fy) + bot * fy).astype(np.float64)
    out[~inside] = np.nan
    return out
