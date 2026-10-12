"""Display-only road layer: roads.display.geojson (synthetic fixtures only)."""

import orjson
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from conftest import road_features, write_synth_bundle
from pev_buddy.api import create_app
from pev_buddy.display_roads import (
    DISPLAY_NAME,
    DISPLAY_PROPS,
    build_display_roads,
    main as display_main,
    write_display_roads,
)
from pev_buddy.sync import _write_display_roads

ALLOWED_PROPS = {"class"}


def _decimals(x: float) -> int:
    s = repr(float(x))
    return len(s.split(".", 1)[1]) if "." in s and "e" not in s else 0


def _assert_display_fc(display: dict, source_features: list) -> None:
    assert display["type"] == "FeatureCollection"
    feats = display["features"]
    assert len(feats) == len(source_features)
    for d, src in zip(feats, source_features):
        assert set(d) == {"type", "properties", "geometry"}, "no feature ids / extra keys"
        assert set(d["properties"]) <= ALLOWED_PROPS
        assert d["properties"]["class"] == src["properties"]["class"]
        assert d["geometry"]["type"] == "LineString"
        src_coords = src["geometry"]["coordinates"]
        assert len(d["geometry"]["coordinates"]) == len(src_coords)
        for (lon, lat), (slon, slat) in zip(d["geometry"]["coordinates"], src_coords):
            assert _decimals(lon) <= 5 and _decimals(lat) <= 5
            assert lon == round(slon, 5) and lat == round(slat, 5)


def test_display_props_match_what_the_frontend_reads():
    assert set(DISPLAY_PROPS) == ALLOWED_PROPS


def test_build_strips_routing_fields_and_rounds_coords():
    src = road_features()
    # the synthetic fixture really is unrounded and routing-heavy
    assert any(_decimals(c) > 5 for f in src for pt in f["geometry"]["coordinates"] for c in pt)
    assert "conns" in src[0]["properties"] and "id" in src[0]
    display = build_display_roads({"type": "FeatureCollection", "features": src})
    _assert_display_fc(display, src)
    blob = orjson.dumps(display)
    for routing_only in (b"conns", b"prohib", b"dest", b"speed", b"ow_moto", b"bike_desig", b"s-main1"):
        assert routing_only not in blob


def test_build_skips_undrawable_and_drops_null_class():
    feats = [
        {"type": "Feature", "properties": {"class": None}, "geometry": {"type": "LineString", "coordinates": [[1.123456, 2.123456], [3, 4]]}},
        {"type": "Feature", "properties": {"class": "residential"}, "geometry": None},
        {"type": "Feature", "properties": {"class": "residential"}, "geometry": {"type": "Point", "coordinates": [1, 2]}},
    ]
    out = build_display_roads({"type": "FeatureCollection", "features": feats})
    assert len(out["features"]) == 1
    assert out["features"][0]["properties"] == {}
    assert out["features"][0]["geometry"]["coordinates"][0] == [1.12346, 2.12346]


def test_write_is_compact_and_smaller(tmp_path):
    data = write_synth_bundle(tmp_path / "data")
    stats = write_display_roads(data / "roads.geojson", data / DISPLAY_NAME)
    raw = (data / DISPLAY_NAME).read_bytes()
    assert b", " not in raw and b": " not in raw and b"\n" not in raw, "display file must be compact JSON"
    assert stats["dst_bytes"] == len(raw) < stats["src_bytes"]
    assert stats["features_in"] == stats["features_out"] == len(road_features())
    assert not (data / (DISPLAY_NAME + ".tmp")).exists()
    _assert_display_fc(orjson.loads(raw), road_features())


def test_cli_derives_display_file_from_existing_roads(tmp_path):
    data = write_synth_bundle(tmp_path / "data")
    res = CliRunner().invoke(display_main, ["--data-dir", str(data)])
    assert res.exit_code == 0, res.output
    assert "display roads:" in res.output
    _assert_display_fc(orjson.loads((data / DISPLAY_NAME).read_bytes()), road_features())


def test_cli_errors_clearly_without_roads(tmp_path):
    res = CliRunner().invoke(display_main, ["--data-dir", str(tmp_path)])
    assert res.exit_code != 0
    assert "roads.geojson" in res.output


def test_sync_helper_writes_display_file(tmp_path):
    data = write_synth_bundle(tmp_path / "data")
    _write_display_roads(data)
    assert (data / DISPLAY_NAME).exists()
    _write_display_roads(tmp_path / "empty")  # no roads.geojson: silently no-op


@pytest.fixture()
def data_with_display(tmp_path):
    data = write_synth_bundle(tmp_path / "data")
    write_display_roads(data / "roads.geojson", data / DISPLAY_NAME)
    return data


def test_api_serves_display_file_with_cache_headers(data_with_display):
    with TestClient(create_app(data_dir=str(data_with_display))) as c:
        r = c.get(f"/data/{DISPLAY_NAME}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/geo+json")
        assert r.headers["cache-control"] == "public, max-age=86400"
        etag = r.headers["etag"]
        assert etag
        _assert_display_fc(r.json(), road_features())

        # ETag revalidation: unchanged file -> 304 with no body
        r2 = c.get(f"/data/{DISPLAY_NAME}", headers={"If-None-Match": etag})
        assert r2.status_code == 304
        assert r2.content == b""
        assert r2.headers["etag"] == etag
        assert r2.headers["cache-control"] == "public, max-age=86400"

        r3 = c.get(f"/data/{DISPLAY_NAME}", headers={"If-None-Match": '"stale"'})
        assert r3.status_code == 200


def test_api_display_404_when_missing_so_frontend_falls_back(synth_data_dir):
    with TestClient(create_app(data_dir=str(synth_data_dir))) as c:
        assert c.get(f"/data/{DISPLAY_NAME}").status_code == 404
        # the full extract is still there for the fallback
        assert c.get("/data/roads.geojson").status_code == 200
