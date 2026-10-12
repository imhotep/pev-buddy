"""Per-vehicle, per-segment ETA (review finding: every route was a flat 15.0 mph)."""

import pytest
from fastapi.testclient import TestClient

from conftest import m_to_lonlat
from pev_buddy import config
from pev_buddy.api import create_app
from pev_buddy.routing import find_route
from pev_buddy.turns import build_steps

V = config.VEHICLE_TYPES
F = config.ETA_REALISM_FACTOR


def implied_mph(r):
    return (r.distance_m / r.duration_s) / config.MPH_TO_MPS


def test_travel_speed_is_min_of_vehicle_and_road_times_factor():
    assert config.travel_speed_mph(V["scooter"], "shared", 25) == pytest.approx(15 * F)
    assert config.travel_speed_mph(V["moped"], "shared", 25) == pytest.approx(25 * F)
    assert config.travel_speed_mph(V["moped"], "arterial", 40) == pytest.approx(30 * F)
    assert config.travel_speed_mph(V["ebike_c3"], "arterial", 40) == pytest.approx(28 * F)
    assert config.travel_speed_mph(V["ebike_c1"], "quiet", 20) == pytest.approx(20 * F)
    # a slow road caps even the fastest vehicle
    assert config.travel_speed_mph(V["moped"], "living_street", 15) == pytest.approx(15 * F)
    # no road limit known: the vehicle cap alone
    assert config.travel_speed_mph(V["ebike_c2"], "bike_lane", None) == pytest.approx(20 * F)


def test_sidewalk_is_walked():
    for vid in ("scooter", "ebike_c3", "moped"):
        assert config.travel_speed_mph(V[vid], "sidewalk", 25) == config.SIDEWALK_WALK_MPH
    assert config.SIDEWALK_WALK_MPH < 15 * F


def test_realism_factor_is_a_sane_urban_discount():
    assert 0.7 <= config.ETA_REALISM_FACTOR <= 0.9


def test_vehicles_get_different_etas_on_the_same_route(graph):
    a, d = m_to_lonlat(0, 0), m_to_lonlat(0, -100)  # one 100 m residential (25 mph) block
    res = {v: find_route(graph, *a, *d, vehicle=v) for v in ("scooter", "ebike_c1", "ebike_c3", "moped")}
    dists = {round(r.distance_m, 3) for r in res.values()}
    assert len(dists) == 1, "same physical route for every vehicle"
    durs = {v: r.duration_s for v, r in res.items()}
    assert durs["moped"] < durs["ebike_c1"] < durs["scooter"]
    assert durs["ebike_c3"] < durs["ebike_c1"]
    assert implied_mph(res["scooter"]) == pytest.approx(15 * F)  # 11.25 mph
    assert implied_mph(res["ebike_c1"]) == pytest.approx(20 * F)  # 15.0 mph
    assert implied_mph(res["moped"]) == pytest.approx(25 * F)  # road-capped: 18.75 mph


def test_moped_faster_than_scooter_and_not_flat_15(graph):
    a, f = m_to_lonlat(0, 0), m_to_lonlat(200, -100)
    s = find_route(graph, *a, *f, vehicle="scooter")
    m = find_route(graph, *a, *f, vehicle="moped")
    assert m.duration_s < s.duration_s
    speeds = {round(implied_mph(find_route(graph, *a, *f, vehicle=v)), 2) for v in config.VEHICLE_ORDER if V[v].routable}
    assert len(speeds) > 1, "ETA must depend on the vehicle"


def test_sidewalk_segment_slows_the_route(graph):
    # Y is only reachable over the 100 m sidewalk s-walk from F.
    a, f, y = m_to_lonlat(0, 0), m_to_lonlat(200, -100), m_to_lonlat(300, -100)
    to_f = find_route(graph, *a, *f, vehicle="ebike_c3")
    to_y = find_route(graph, *a, *y, vehicle="ebike_c3")
    assert "sidewalk" in to_y.segment_stats
    walk_m = to_y.segment_stats["sidewalk"]["distance_m"]
    walk_s = walk_m / (config.SIDEWALK_WALK_MPH * config.MPH_TO_MPS)
    assert to_y.duration_s - to_f.duration_s == pytest.approx(walk_s, rel=0.02)
    # walking 100 m takes longer than riding the ~300 m before it
    assert walk_s > to_f.duration_s


def test_route_duration_is_sum_of_edges_and_steps(graph):
    a, y = m_to_lonlat(0, 0), m_to_lonlat(300, -100)
    for vid in ("scooter", "ebike_c3", "moped"):
        r = find_route(graph, *a, *y, vehicle=vid)
        expected = sum(
            config.edge_duration_s(V[vid], graph.edges[e][4], graph.edges[e][5], graph.edges[e][6])
            for e in r.edge_idxs
        )
        assert r.duration_s == pytest.approx(expected)
        steps = build_steps(graph, r.edge_idxs, vehicle=vid)
        assert sum(s["duration_s"] for s in steps) == pytest.approx(r.duration_s)


@pytest.fixture()
def client(synth_data_dir):
    with TestClient(create_app(data_dir=str(synth_data_dir))) as c:
        yield c


def test_api_route_eta_depends_on_vehicle(client):
    a, d = m_to_lonlat(0, 0), m_to_lonlat(0, -100)
    body = lambda v: client.post(
        "/api/route",
        json={"start": {"lon": a[0], "lat": a[1]}, "end": {"lon": d[0], "lat": d[1]}, "vehicle": v},
    ).json()
    s, m = body("scooter"), body("moped")
    assert s["distance_m"] == m["distance_m"]
    assert m["duration_s"] < s["duration_s"]
    assert sum(st["duration_s"] for st in m["steps"]) == pytest.approx(m["duration_s"], abs=len(m["steps"]))
