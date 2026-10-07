"""Display-only road layer for the browser map.

``roads.geojson`` carries every routing field (``conns`` adjacency,
``prohib``, ``dest``, ``speed``, one-way flags, UUID ids, ...), but routing
runs server-side from the graph bundle and the map style only reads
``class``. This module derives ``roads.display.geojson`` — same geometry,
properties cut to :data:`DISPLAY_PROPS`, coordinates rounded to 5 decimals
(~1 m), no feature ids, compact JSON — which is what the frontend downloads.

``sync`` writes it automatically. To (re)build it from an existing
``roads.geojson`` without a full, RAM-heavy sync (e.g. on the VPS):

    python -m pev_buddy.display_roads              # uses ./data (or $PEV_BUDDY_DATA)
    python -m pev_buddy.display_roads --data-dir /path/to/data
"""

from __future__ import annotations

import os
from pathlib import Path

import click
import orjson

from . import config

# Feature properties the frontend actually reads (web/app.js: the "pev-roads"
# layer's paint expressions use ["get", "class"]; nothing else touches road
# features). Add a key here only if the map style starts reading it.
DISPLAY_PROPS: tuple[str, ...] = ("class",)
COORD_DECIMALS = 5

SOURCE_NAME = "roads.geojson"
DISPLAY_NAME = "roads.display.geojson"


def _round_line(coords) -> list:
    return [[round(pt[0], COORD_DECIMALS), round(pt[1], COORD_DECIMALS)] for pt in coords]


def display_feature(feat: dict) -> dict | None:
    """Strip one road feature down to what the map draws (None if undrawable)."""
    geom = feat.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return None
    if gtype == "LineString":
        out_coords = _round_line(coords)
    elif gtype == "MultiLineString":
        out_coords = [_round_line(line) for line in coords]
    else:
        return None
    props = feat.get("properties") or {}
    return {
        "type": "Feature",
        "properties": {k: props[k] for k in DISPLAY_PROPS if props.get(k) is not None},
        "geometry": {"type": gtype, "coordinates": out_coords},
    }


def build_display_roads(roads: dict) -> dict:
    """Return the display FeatureCollection for a full ``roads.geojson`` dict."""
    feats = []
    for f in roads.get("features", []):
        d = display_feature(f)
        if d is not None:
            feats.append(d)
    return {"type": "FeatureCollection", "features": feats}


def write_display_roads(src: Path, dst: Path) -> dict:
    """Read ``src`` (full roads.geojson), write ``dst`` atomically; return stats."""
    src, dst = Path(src), Path(dst)
    raw = src.read_bytes()
    src_bytes = len(raw)
    roads = orjson.loads(raw)
    del raw
    display = build_display_roads(roads)
    n_in = len(roads.get("features", []))
    del roads
    out = orjson.dumps(display)  # orjson output is already compact
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(out)
    os.replace(tmp, dst)  # never leave a half-written file for the server
    return {
        "features_in": n_in,
        "features_out": len(display["features"]),
        "src_bytes": src_bytes,
        "dst_bytes": len(out),
    }


@click.command()
@click.option(
    "--data-dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory holding roads.geojson (default: ./data or $PEV_BUDDY_DATA)",
)
def main(data_dir: Path | None) -> None:
    """Derive data/roads.display.geojson from data/roads.geojson."""
    d = data_dir or config.DATA_DIR
    src = d / SOURCE_NAME
    if not src.exists():
        raise click.ClickException(f"{src} not found — run `python -m pev_buddy.sync` first")
    s = write_display_roads(src, d / DISPLAY_NAME)
    click.echo(
        f"display roads: {s['features_out']}/{s['features_in']} features, "
        f"{s['src_bytes'] / 1e6:.1f} MB -> {s['dst_bytes'] / 1e6:.1f} MB -> {d / DISPLAY_NAME}"
    )


if __name__ == "__main__":
    main()
