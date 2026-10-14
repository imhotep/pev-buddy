"""Trips start and end at the nearest point on a street, not its nearest
junction (#51: a 34 m hop on one-way 3rd St was routed as a loop)."""

import pytest

from conftest import _seg, m_to_lonlat
from pev_buddy import routing
from pev_buddy.graph import RoadGraph
from pev_buddy.turns import build_steps

# A 100 m square: the north side is one-way eastbound, the rest two-way.
NODES = {"A": (0, 0), "B": (100, 0), "C": (100, -100), "D": (0, -100)}


def _square():
    feats = [
        _seg("n", "North Street", "residential", [(0, 0), (100, 0)], ow=1, ow_moto=1, conns=[["conn-A", 0.0], ["conn-B", 1.0]]),
        _seg("e", "East Street", "residential", [(100, 0), (100, -100)], conns=[["conn-B", 0.0], ["conn-C", 1.0]]),
        _seg("s", "South Street", "residential", [(100, -100), (0, -100)], conns=[["conn-C", 0.0], ["conn-D", 1.0]]),
        _seg("w", "West Street", "residential", [(0, -100), (0, 0)], conns=[["conn-D", 0.0], ["conn-A", 1.0]]),
    ]
    g = RoadGraph()
    g.build({"type": "FeatureCollection", "features": feats}, {f"conn-{k}": list(m_to_lonlat(*p)) for k, p in NODES.items()})
    return g


def _route(g, a, b, vehicle="ebike_c3"):
    return routing.find_route(g, *m_to_lonlat(*a), *m_to_lonlat(*b), vehicle=vehicle)


def test_a_hop_along_one_block_rides_just_that_stretch():
    r = _route(_square(), (30, 2), (70, 2))
    assert r.distance_m == pytest.approx(40, abs=1)
    assert len(r.edge_idxs) == 1 and r.edge_parts[0] == (pytest.approx(0.3, abs=0.01), pytest.approx(0.7, abs=0.01))
    assert r.start_snap_m == pytest.approx(2, abs=0.2)


def test_against_a_one_way_it_goes_around_the_block():
    r = _route(_square(), (70, 2), (30, 2))
    # 30 m on to B, round the other three sides, 30 m in from A.
    assert r.distance_m == pytest.approx(360, abs=2)


def test_a_two_way_street_can_be_ridden_either_way_from_mid_block():
    r = _route(_square(), (100, -70), (100, -30))  # north on East Street
    assert r.distance_m == pytest.approx(40, abs=1)
    r = _route(_square(), (100, -30), (100, -70))  # and south
    assert r.distance_m == pytest.approx(40, abs=1)


def test_same_spot_mid_block_is_zero():
    r = _route(_square(), (50, -100), (50, -100))
    assert r.distance_m == pytest.approx(0, abs=0.01)


def test_a_point_at_a_corner_starts_at_the_junction():
    g = _square()
    r = _route(g, (0, 1), (100, -50))
    assert r.edge_parts[0][0] == 0.0
    assert g.edges[r.edge_idxs[0]][0] == g.node_index["conn-A"]


def test_path_and_steps_start_and_end_at_the_points():
    g = _square()
    r = _route(g, (30, 2), (100, -60))
    slon, slat = m_to_lonlat(30, 0)
    elon, elat = m_to_lonlat(100, -60)
    assert r.path[0] == [pytest.approx(slon, abs=1e-6), pytest.approx(slat, abs=1e-6)]
    assert r.path[-1] == [pytest.approx(elon, abs=1e-6), pytest.approx(elat, abs=1e-6)]
    assert r.distance_m == pytest.approx(70 + 60, abs=1)
    steps = build_steps(g, r.edge_idxs, "Cafe", edge_parts=r.edge_parts)
    assert [s["maneuver"] for s in steps] == ["depart", "right", "arrive"]
    assert steps[0]["distance_m"] == pytest.approx(70, abs=1)
    assert steps[1]["distance_m"] == pytest.approx(60, abs=1)
    assert steps[-1]["geometry"][0] == [pytest.approx(elon, abs=1e-6), pytest.approx(elat, abs=1e-6)]
    assert sum(s["distance_m"] for s in steps) == pytest.approx(r.distance_m, abs=0.5)


def test_api_short_hop_matches_the_pins(synth_data_dir):
    # The #51 shape through the API: two taps 34 m apart along one block.
    from fastapi.testclient import TestClient

    from pev_buddy.api import create_app

    def pt(dx, dy):
        lon, lat = m_to_lonlat(dx, dy)
        return {"lat": lat, "lon": lon}

    with TestClient(create_app(data_dir=str(synth_data_dir))) as c:
        body = c.post("/api/route", json={"start": pt(30, 3), "end": pt(64, 3), "vehicle": "scooter"}).json()
    assert body["distance_m"] == pytest.approx(34, abs=1)
    assert [s["maneuver"] for s in body["steps"]] == ["depart", "arrive"]
    assert body["steps"][0]["distance_m"] == pytest.approx(34, abs=1)
    assert not any("snapped" in w for w in body["warnings"])
