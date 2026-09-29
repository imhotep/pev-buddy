"""Tests for the CA vehicle types: config data, road access, and cost profiles."""

import pytest

from conftest import _seg, make_road_graph, m_to_lonlat, road_features
from pev_buddy import config
from pev_buddy.overture_fetch import effective_max_speed_mph
from pev_buddy.routing import find_route


def edge_idxs(graph, sid):
    """Directed edge indices belonging to segment id `sid`."""
    idx = next(i for i, s in enumerate(graph.segments) if s["id"] == sid)
    return [e for e in range(graph.edge_count) if graph.edges[e][2] == idx]


def single_road_graph(cls="secondary", speed=None, **extra):
    """A graph of one A-B segment with the given class/posted speed."""
    feats = [_seg("s-road", "Road", cls, [(0, 0), (100, 0)], speed=speed, **extra,
                  conns=[["conn-A", 0.0], ["conn-B", 1.0]])]
    return make_road_graph(features=feats)


# --- config data -------------------------------------------------------------


def test_vehicle_config_data():
    for vid in ("scooter", "emb", "euc", "ebike_c1", "ebike_c2", "ebike_c3", "moped", "emoto"):
        v = config.VEHICLE_TYPES[vid]
        assert v.id == vid and v.label and v.description
    assert config.VEHICLE_TYPES["emoto"].routable is False
    for vid in ("scooter", "emb", "euc", "ebike_c1", "ebike_c2", "ebike_c3", "moped"):
        assert config.VEHICLE_TYPES[vid].routable is True
    assert config.DEFAULT_VEHICLE == "scooter"
    # EUCs route exactly like e-scooters (same access data)
    euc, scooter = config.VEHICLE_TYPES["euc"], config.VEHICLE_TYPES["scooter"]
    assert (euc.road_max_mph, euc.road_max_inclusive, euc.bike_lane_exempt, euc.profile) == (
        scooter.road_max_mph,
        scooter.road_max_inclusive,
        scooter.bike_lane_exempt,
        scooter.profile,
    )
    # road-access rules live in config as data
    assert config.VEHICLE_TYPES["scooter"].road_max_mph == 25
    assert config.VEHICLE_TYPES["emb"].road_max_mph == 35
    assert config.VEHICLE_TYPES["emb"].road_max_inclusive is False  # "under 35 mph"
    for vid in ("ebike_c1", "ebike_c2", "ebike_c3", "moped"):
        assert config.VEHICLE_TYPES[vid].road_max_mph is None  # no road-speed restriction
    # mopeds ride under Overture's motorcycle access rules
    assert config.VEHICLE_TYPES["moped"].access_mode == "motorcycle"
    assert config.VEHICLE_TYPES["scooter"].access_mode == "bicycle"


# --- road access per vehicle type --------------------------------------------


def test_scooter_road_limit_25_mph():
    g = single_road_graph(speed=40)
    for e in edge_idxs(g, "s-road"):
        for vid in ("scooter", "euc", "emb"):
            assert not g.edge_ok[vid][e], f"{vid} may not ride a 40 mph road"
        for vid in ("ebike_c1", "ebike_c2", "ebike_c3", "moped"):
            assert g.edge_ok[vid][e], f"{vid} should ride a 40 mph road"
    # 25 mph is inclusive for the scooter
    g = single_road_graph(speed=25)
    for e in edge_idxs(g, "s-road"):
        assert g.edge_ok["scooter"][e]
        assert g.edge_ok["emb"][e]


def test_emb_strictly_under_35_mph():
    g = single_road_graph(speed=34)
    for e in edge_idxs(g, "s-road"):
        assert g.edge_ok["emb"][e], "34 mph is under 35"
    g = single_road_graph(speed=35)
    for e in edge_idxs(g, "s-road"):
        assert not g.edge_ok["emb"][e], "35 mph is not under 35"
        assert g.edge_ok["ebike_c1"][e]


def test_bike_lane_exempt_above_road_limit():
    # a 40 mph secondary with a designated bicycle facility: the boards may
    # ride it in the lane, even though 40 > their road limit
    g = single_road_graph(speed=40, bike_desig=3)
    for e in edge_idxs(g, "s-road"):
        for vid in ("scooter", "euc", "emb", "ebike_c1", "moped"):
            assert g.edge_ok[vid][e]


def test_sidewalk_and_living_street_have_no_road_limit():
    g = single_road_graph(cls="footway")
    for e in edge_idxs(g, "s-road"):
        for vid in ("scooter", "emb", "euc", "ebike_c1", "moped"):
            assert g.edge_ok[vid][e]
    g = single_road_graph(cls="living_street")
    for e in edge_idxs(g, "s-road"):
        for vid in ("scooter", "emb", "euc", "ebike_c1", "moped"):
            assert g.edge_ok[vid][e]


def test_vehicle_blocked_from_fast_only_connection():
    # two nodes joined only by a 40 mph road: e-bikes cross it; the boards
    # (scooter <= 25, emb < 35) cannot
    from pev_buddy.routing import RouteError

    g = single_road_graph(speed=40)
    a, b = m_to_lonlat(0, 0), m_to_lonlat(100, 0)
    r = find_route(g, *a, *b, vehicle="ebike_c1", snap_radius_m=50)
    assert "arterial" in r.segment_stats
    for vid in ("scooter", "euc", "emb"):
        with pytest.raises(RouteError):
            find_route(g, *a, *b, vehicle=vid, snap_radius_m=50)


# --- cost profiles -------------------------------------------------------------


def test_cost_profiles_per_vehicle_type():
    g = make_road_graph()
    e_fast = edge_idxs(g, "s-fast")[0]  # 40 mph arterial
    length = g.edges[e_fast][4]
    assert g.edge_cost(e_fast, "scooter") == pytest.approx(length * config.COST_PROFILES["A"]["arterial"])
    assert g.edge_cost(e_fast, "ebike_c3") == pytest.approx(length * config.COST_PROFILES["B"]["arterial"])
    assert g.edge_cost(e_fast, "moped") == pytest.approx(length * config.COST_PROFILES["C"]["arterial"])

    e_main = edge_idxs(g, "s-main1")[0]  # residential -> shared
    length = g.edges[e_main][4]
    assert g.edge_cost(e_main, "scooter") == pytest.approx(length * config.COST_PROFILES["A"]["shared"])
    assert g.edge_cost(e_main, "ebike_c3") == pytest.approx(length * config.COST_PROFILES["B"]["shared"])
    assert g.edge_cost(e_main, "moped") == pytest.approx(length * config.COST_PROFILES["C"]["shared"])

    # dedicated bike lanes are discouraged for mopeds (usually bicycle-only)
    e_cycle = edge_idxs(g, "s-vert2")[0]
    assert g.edge_cost(e_cycle, "scooter") < g.edge_cost(e_cycle, "moped")


def test_moped_honors_motorcycle_denials():
    # s-slow is a 20 mph road everyone may ride; deny mopeds the forward
    # heading via the motorcycle access mask
    feats = road_features()
    for f in feats:
        if f["properties"]["id"] == "s-slow":
            f["properties"]["ow_moto"] = 2  # motorcycles backward only
    g = make_road_graph(features=feats)
    assert len(edge_idxs_by_mask(g, "s-slow", "scooter")) == 2, "scooters ignore motorcycle denials"
    assert len(edge_idxs_by_mask(g, "s-slow", "moped")) == 1, "mopeds honor motorcycle denials"


def edge_idxs_by_mask(graph, sid, vehicle):
    """Edges of `sid` that `vehicle` may actually ride (access-masked)."""
    return [e for e in edge_idxs(graph, sid) if graph.edge_ok[vehicle][e]]


# --- categories, signposts, speed limits ---------------------------------------


def test_road_category_banding():
    # check the category banding directly through _edge_category
    from pev_buddy.graph import RoadGraph

    cat = RoadGraph._edge_category
    assert cat("residential", None, False) == "shared"  # 25 mph default
    assert cat("service", None, False) == "quiet"  # 20 mph default
    assert cat("unclassified", None, False) == "shared"  # 25 mph default
    assert cat("tertiary", None, False) == "arterial"  # 30 mph default
    assert cat("secondary", None, False) == "arterial"  # 40 mph default
    assert cat("living_street", None, False) == "living_street"
    assert cat("footway", None, False) == "sidewalk"
    assert cat("cycleway", None, False) == "bike_lane"
    assert cat("motorway", None, False) is None  # 65 mph default -> excluded
    assert cat("steps", None, False) is None
    assert cat("residential", 35, False) == "arterial"  # posted limit wins
    assert cat("residential", 10, False) == "quiet"
    assert cat("residential", 50, False) is None  # above the hard ceiling


def test_designated_bicycle_rule_is_a_bike_lane():
    feats = road_features()
    for f in feats:
        if f["properties"]["id"] == "s-side1":
            f["properties"]["bike_desig"] = 3
    g = make_road_graph(features=feats)
    for e in edge_idxs(g, "s-side1"):
        assert g.edges[e][5] == "bike_lane"


def test_speed_limits_mode_filtered():
    # an hgv-only limit must not describe our speed; the general one counts
    rules = [
        {"max_speed": {"value": 60, "unit": "mph"}, "when": {"mode": ["hgv"]}},
        {"max_speed": {"value": 40, "unit": "km/h"}, "between": [0.0, 1.0]},
    ]
    assert effective_max_speed_mph(rules) == int(round(40 * 0.621371))
    # hgv-only alone -> nothing applicable
    assert effective_max_speed_mph([rules[0]]) is None
    # a bicycle-scoped limit does apply
    assert effective_max_speed_mph(
        [{"max_speed": {"value": 15, "unit": "mph"}, "when": {"mode": ["bicycle"]}}]
    ) == 15


# --- segment stats --------------------------------------------------------------


def test_segment_stats_breakdown():
    g = make_road_graph()
    a = m_to_lonlat(0, 0)
    h = m_to_lonlat(-100, 0)
    r = find_route(g, *a, *h, vehicle="ebike_c1")  # straight onto the 40 mph arterial
    assert r.segment_stats["arterial"]["max_speed_mph"] == 40
    assert 95 < r.segment_stats["arterial"]["distance_m"] < 105
    assert r.segment_stats["arterial"]["distance_m"] == pytest.approx(r.distance_m)


def test_segment_stats_multiple_categories():
    g = make_road_graph()
    a = m_to_lonlat(0, 0)
    f = m_to_lonlat(200, -100)
    r = find_route(g, *a, *f, vehicle="ebike_c3")  # shared streets + cycleway
    assert "bike_lane" in r.segment_stats
    assert "shared" in r.segment_stats
    total = sum(v["distance_m"] for v in r.segment_stats.values())
    assert total == pytest.approx(r.distance_m, abs=1e-6)


def test_moped_avoids_dedicated_bike_lane():
    # moped profile prices bike lanes at 3x, so the all-residential path wins
    g = make_road_graph()
    a = m_to_lonlat(0, 0)
    f = m_to_lonlat(200, -100)
    r = find_route(g, *a, *f, vehicle="moped")
    assert "bike_lane" not in r.segment_stats
    # ...while the e-scooter takes the cycleway
    r = find_route(g, *a, *f, vehicle="scooter")
    assert "bike_lane" in r.segment_stats
