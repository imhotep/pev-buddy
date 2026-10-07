"""CLI entry point: `python -m pev_buddy.sync` (or `pev-buddy`) to extract Overture data."""

from __future__ import annotations

import json
from pathlib import Path

import click
import orjson

from . import bikelink_fetch, config, overture_fetch, racks_fetch


@click.group()
def main():
    """PEV Buddy — Overture Maps data tools for San Francisco."""


def _write_graph_bundle(out_dir: Path) -> None:
    """Build the road graph once at sync time and write graph.npz + graph_meta.json.

    The server loads the bundle directly, so the 39 MB geojson parse (and its
    memory spike) happens here, on the build machine, instead of at boot.
    """
    from .graph import RoadGraph

    roads_p, conns_p = out_dir / "roads.geojson", out_dir / "connectors.json"
    if not (roads_p.exists() and conns_p.exists()):
        click.echo("bundle: roads/connectors missing; keeping existing bundle", err=True)
        return
    g = RoadGraph()
    g.build(orjson.loads(roads_p.read_bytes()), orjson.loads(conns_p.read_bytes()))
    g.save_bundle(out_dir)
    click.echo(f"bundle: graph.npz + graph_meta.json ({g.node_count} nodes, {g.edge_count} edges)")


def _write_display_roads(out_dir: Path) -> None:
    """Write the display-only roads.display.geojson the browser map downloads."""
    from .display_roads import DISPLAY_NAME, SOURCE_NAME, write_display_roads

    roads_p = out_dir / SOURCE_NAME
    if not roads_p.exists():
        return
    try:
        s = write_display_roads(roads_p, out_dir / DISPLAY_NAME)
    except Exception as e:  # the map falls back to roads.geojson; never fail sync
        click.echo(f"display roads: failed ({e}); frontend will fall back to roads.geojson", err=True)
        return
    click.echo(
        f"display roads: {DISPLAY_NAME} ({s['features_out']} features, "
        f"{s['src_bytes'] / 1e6:.1f} MB -> {s['dst_bytes'] / 1e6:.1f} MB)"
    )


def _write_addresses_bundle(out_dir: Path) -> None:
    """Convert addresses.parquet to the packed addresses.npz + _meta.json bundle."""
    import numpy as np

    from .geocode import _read_rows, pack_addresses

    pq_path = out_dir / "addresses.parquet"
    if not pq_path.exists():
        return
    arrays, meta = pack_addresses(_read_rows(pq_path))
    np.savez(out_dir / "addresses.npz", **arrays)
    (out_dir / "addresses_meta.json").write_bytes(orjson.dumps(meta))
    click.echo("bundle: addresses.npz + addresses_meta.json")


@main.command()
@click.option("--bbox", default=None, help="xmin,ymin,xmax,ymax (default: SF)")
@click.option("--release", default=None, help=f"Overture release (default: {config.OVERTURE_RELEASE})")
@click.option("--out-dir", default=None, type=click.Path(), help="Output directory (default: ./data)")
@click.option("--skip", default="", help="Comma-separated: roads,connectors,stations,addresses,places,bikelink,racks")
def sync(bbox: str | None, release: str | None, out_dir: str | None, skip: str):
    """Download the SF Overture slices + BikeLink lockers + SFMTA bike racks."""
    parsed_bbox = None
    if bbox:
        parsed_bbox = tuple(float(v) for v in bbox.split(","))
        if len(parsed_bbox) != 4:
            raise click.UsageError("--bbox must be xmin,ymin,xmax,ymax")
    skip_set = {s.strip() for s in skip.split(",") if s.strip()}
    manifest = overture_fetch.sync_all(
        bbox=parsed_bbox,
        release=release,
        out_dir=Path(out_dir) if out_dir else None,
        skip=skip_set,
    )
    bbox4 = parsed_bbox or config.SF_BBOX
    out_dir_p = Path(out_dir) if out_dir else config.DATA_DIR
    if "roads" not in skip_set and "connectors" not in skip_set:
        _write_graph_bundle(out_dir_p)
    if "roads" not in skip_set:
        _write_display_roads(out_dir_p)
    if "addresses" not in skip_set:
        _write_addresses_bundle(out_dir_p)
    if "bikelink" not in skip_set:
        try:
            n = bikelink_fetch.extract_bikelink(bbox4, out_dir_p / "bikelink.geojson")
            manifest.setdefault("counts", {})["bikelink"] = n
            (out_dir_p / "manifest.json").write_text(json.dumps(manifest, indent=2))
        except Exception as e:
            click.echo(f"bikelink: extraction failed ({e}); any existing slice is kept", err=True)
    if "racks" not in skip_set:
        try:
            n = racks_fetch.extract_racks(bbox4, out_dir_p / "racks.geojson")
            manifest.setdefault("counts", {})["racks"] = n
            (out_dir_p / "manifest.json").write_text(json.dumps(manifest, indent=2))
        except Exception as e:
            click.echo(f"racks: extraction failed ({e}); any existing slice is kept", err=True)
    click.echo(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 1 or sys.argv[1].startswith("-"):
        sys.argv.insert(1, "sync")
    main()
