"""Walking directions: from a parking spot to the real destination."""

import pytest
from fastapi.testclient import TestClient

from conftest import NODES, m_to_lonlat, make_road_graph
from pev_buddy import routing
from pev_buddy.api import create_app


@pytest.fixture()
def client(synth_data_dir):
    with TestClient(create_app(data_dir=str(synth_data_dir))) as c:
        yield c


def _walk(g, a, b):
    (alon, alat), (blon, blat) = m_to_lonlat(*NODES[a]), m_to_lonlat(*NODES[b])
    return routing.find_walk(g, alon, alat, blon, blat)


def test_walk_takes_stairs_vehicles_cannot():
    # X is reachable only up the stairs from F: no vehicle route, a walk.
    g = make_road_graph()
    flon, flat = m_to_lonlat(*NODES["F"])
    xlon, xlat = m_to_lonlat(*NODES["X"])
    ride = routing.find_route(g, flon, flat, xlon, xlat, vehicle="ebike_c3")
    assert ride.end_node != g.node_index["conn-X"]  # snapped short of the stairs
    w = _walk(g, "F", "X")
    assert w.distance_m == pytest.approx(50, abs=1)
    assert w.duration_s == pytest.approx(50 / (3 * 0.44704), rel=0.01)


def test_walk_goes_against_a_one_way():
    # The alley D -> G is one-way for bikes; walking G -> D uses it backwards.
    g = make_road_graph()
    w = _walk(g, "G", "D")
    assert w.distance_m == pytest.approx(100, abs=1)
    glon, glat = m_to_lonlat(*NODES["G"])
    dlon, dlat = m_to_lonlat(*NODES["D"])
    assert w.path[0] == pytest.approx([glon, glat], abs=1e-5)
    assert w.path[-1] == pytest.approx([dlon, dlat], abs=1e-5)


def test_walk_api(client):
    alon, alat = m_to_lonlat(*NODES["F"])
    xlon, xlat = m_to_lonlat(*NODES["X"])
    r = client.post("/api/walk", json={"start": {"lat": alat, "lon": alon}, "end": {"lat": xlat, "lon": xlon}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["distance_m"] == pytest.approx(50, abs=1) and len(body["path"]) >= 2
    far = client.post("/api/walk", json={"start": {"lat": alat, "lon": alon}, "end": {"lat": alat + 1, "lon": alon}})
    assert far.status_code == 400
