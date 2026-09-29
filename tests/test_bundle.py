"""Bundle round-trip and parity: bundle-loaded graph/app must behave identically."""

import numpy as np
import orjson
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from conftest import m_to_lonlat, make_road_graph, road_features
from pev_buddy import config
from pev_buddy.api import create_app
from pev_buddy.geocode import _read_rows, pack_addresses
from pev_buddy.graph import RoadGraph
from pev_buddy.routing import find_route
from pev_buddy.turns import build_steps

A = m_to_lonlat(0, 0)
F = m_to_lonlat(200, -100)
Y = m_to_lonlat(300, -100)


def test_bundle_roundtrip(tmp_path):
    g = make_road_graph()
    g.save_bundle(tmp_path)
    g2 = RoadGraph.load_bundle(tmp_path)

    assert g2.node_count == g.node_count
    assert g2.edge_count == g.edge_count
    assert g2.edges == g.edges
    assert g2.node_index == g.node_index
    assert [s["id"] for s in g2.segments] == [s["id"] for s in g.segments]
    assert [s["name"] for s in g2.segments] == [s["name"] for s in g.segments]
    assert [s["class"] for s in g2.segments] == [s["class"] for s in g.segments]
    assert [s["conns"] for s in g2.segments] == [s["conns"] for s in g.segments]
    assert g2.prohib == g.prohib
    assert g2.dest == g.dest
    assert g2.edge_counts == g.edge_counts
    for v in g.edge_ok:
        assert bytes(g2.edge_ok[v]) == bytes(g.edge_ok[v])
        assert bytes(g2.out_ok[v]) == bytes(g.out_ok[v])
        assert bytes(g2.in_ok[v]) == bytes(g.in_ok[v])
    assert list(g2.scc) == list(g.scc)
    assert g2.scc_reach == g.scc_reach
    for e in range(g.edge_count):
        assert g2.edge_geometry(e) == g.edge_geometry(e)


def test_bundle_routes_identical(tmp_path):
    g = make_road_graph()
    g.save_bundle(tmp_path)
    g2 = RoadGraph.load_bundle(tmp_path)
    for veh in ("scooter", "ebike_c3", "moped"):
        for start, end in ((A, F), (A, Y), (F, A)):
            r1 = find_route(g, *start, *end, vehicle=veh)
            r2 = find_route(g2, *start, *end, vehicle=veh)
            assert r1.edge_idxs == r2.edge_idxs
            assert r1.path == r2.path
            assert r1.distance_m == r2.distance_m
            assert r1.duration_s == r2.duration_s
            assert r1.segment_stats == r2.segment_stats
            assert build_steps(g, r1.edge_idxs, "dest") == build_steps(g2, r2.edge_idxs, "dest")


def _write_fixture_data(data):
    data.mkdir()
    feats = road_features()
    (data / "roads.geojson").write_bytes(orjson.dumps({"type": "FeatureCollection", "features": feats}))
    connectors = {f"conn-{n}": list(m_to_lonlat(*p)) for n, p in {
        "A": (0, 0), "B": (100, 0), "C": (200, 0), "D": (0, -100), "E": (100, -100),
        "F": (200, -100), "G": (0, -200), "H": (-100, 0), "H2": (-100, -100), "X": (250, -100),
        "Y": (300, -100), "I": (-100, 100), "J": (-100, -200),
    }.items()}
    (data / "connectors.json").write_bytes(orjson.dumps(connectors))
    alon, alat = m_to_lonlat(0, 0)
    (data / "stations.geojson").write_bytes(orjson.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "id": "st-1",
            "properties": {"name": "Test Charger", "brand": "EVgo", "address": "123 Test Way",
                           "phone": None, "website": None, "confidence": 0.9},
            "geometry": {"type": "Point", "coordinates": [alon, alat]},
        }],
    }))
    table = pa.table({
        "street": ["MAIN ST", "SIDE ST"],
        "number": ["5", "10"],
        "unit": [None, None],
        "postcode": ["94102", "94102"],
        "lon": [m_to_lonlat(0, 0)[0], m_to_lonlat(0, -100)[0]],
        "lat": [m_to_lonlat(0, 0)[1], m_to_lonlat(0, -100)[1]],
    })
    pq.write_table(table, data / "addresses.parquet")
    (data / "places.json").write_bytes(orjson.dumps([
        ["p-1", "Test Bakery", "restaurant", "12 Test Way", m_to_lonlat(0, 0)[0], m_to_lonlat(0, 0)[1]],
        ["p-2", "Side Street Deli", "casual_eatery", "10 Side St", m_to_lonlat(0, -100)[0], m_to_lonlat(0, -100)[1]],
    ]))
    (data / "manifest.json").write_text('{"release": "test", "counts": {}}')


def _responses(client):
    out = {}
    out["health"] = client.get("/api/health").json()
    out["health"].pop("build_seconds", None)
    out["search"] = client.get("/api/search", params={"q": "main st"}).json()
    out["geocode"] = client.get("/api/geocode", params={"q": "5 Main St"}).json()
    for veh in ("scooter", "ebike_c3", "moped"):
        r = client.post("/api/route", json={
            "start": {"query": "Main St"},
            "end": {"lon": F[0], "lat": F[1], "label": "dest"},
            "vehicle": veh,
        })
        assert r.status_code == 200, r.text
        out[f"route_{veh}"] = r.json()
    out["roads_geojson"] = client.get("/data/roads.geojson").content
    return out


def test_api_parity_bundle_vs_geojson(tmp_path):
    data = tmp_path / "data"
    _write_fixture_data(data)

    with TestClient(create_app(data_dir=str(data))) as c:
        plain = _responses(c)

    # Add bundle artifacts derived from the same raw slices.
    g = RoadGraph()
    g.build(
        orjson.loads((data / "roads.geojson").read_bytes()),
        orjson.loads((data / "connectors.json").read_bytes()),
    )
    g.save_bundle(data)
    arrays, meta = pack_addresses(_read_rows(data / "addresses.parquet"))
    np.savez(data / "addresses.npz", **arrays)
    (data / "addresses_meta.json").write_bytes(orjson.dumps(meta))

    with TestClient(create_app(data_dir=str(data))) as c:
        bundled = _responses(c)

    assert bundled == plain
