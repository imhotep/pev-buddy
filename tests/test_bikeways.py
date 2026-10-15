"""SFMTA bikeways matched onto the road graph, and the avoid-traffic routing
for e-boards and Onewheels (profile D)."""

import pytest

from conftest import NODES, _seg, m_to_lonlat, road_features
from pev_buddy import bikeways, config
from pev_buddy.graph import RoadGraph
from pev_buddy.routing import find_route


def _line(cls, pts, direction=None):
    return {
        "type": "Feature",
        "properties": {"cls": cls, "street": None, "dir": direction},
        "geometry": {"type": "LineString", "coordinates": [list(m_to_lonlat(x, y)) for x, y in pts]},
    }


def _graph(features, lanes=None, nodes=NODES):
    g = RoadGraph()
    sfmta = bikeways.match(features, lanes) if lanes is not None else None
    g.build({"type": "FeatureCollection", "features": features}, {f"conn-{k}": list(m_to_lonlat(*p)) for k, p in nodes.items()}, None, sfmta)
    return g


def _cats(g, seg_id):
    """{heading: category} for a segment's edges."""
    idx = next(i for i, s in enumerate(g.segments) if s["id"] == seg_id)
    return {e[3]: e[5] for e in g.edges if e[2] == idx}


def test_to_feature_keeps_class_and_direction():
    row = {
        "properties": {"FACILITY_T": "CLASS II", "STREETNAME": "BRANNAN ST", "DIR": "E ", "DIRECT": "2W"},
        "geometry": {"type": "LineString", "coordinates": [[-122.4, 37.77], [-122.399, 37.771]]},
    }
    f = bikeways.to_feature(row)
    assert f["properties"] == {"cls": 2, "street": "BRANNAN ST", "dir": "E"}
    assert bikeways.to_feature({**row, "properties": {"FACILITY_T": None}}) is None


def test_match_puts_each_class_on_its_street_and_direction():
    feats = road_features()
    lanes = [
        _line(2, [(0, 2), (200, 2)]),  # painted lanes both ways along Main Street
        _line(4, [(0, -98), (200, -98)], "E"),  # protected, eastbound only, on Side Street
        _line(3, [(1, 0), (1, -100)]),  # a signed route on Cross Street
        _line(2, [(200, -103), (300, -103)]),  # a street lane beside the Walkway: not the sidewalk's
        _line(2, [(-100, 40), (0, 40)]),  # 40 m off Fast Road: no match
    ]
    m = bikeways.match(feats, lanes)
    assert m["s-main1"] == m["s-main2"] == (2, 2)
    assert m["s-side1"] == m["s-side2"] == (4, 0)
    assert m["s-vert1"] == (3, 3)
    assert "s-walk" not in m and "s-fast" not in m


def test_sfmta_classes_become_categories():
    feats = road_features()
    lanes = [_line(2, [(0, 2), (200, 2)]), _line(4, [(0, -98), (200, -98)], "E"), _line(3, [(1, 0), (1, -100)])]
    g = _graph(feats, lanes)
    assert _cats(g, "s-main1") == {0: "bike_lane", 1: "bike_lane"}
    assert _cats(g, "s-side1") == {0: "bike_path", 1: "shared"}  # protected eastbound only
    assert _cats(g, "s-vert1") == {0: "shared", 1: "shared"}  # sharrows: with cars
    assert set(_cats(g, "s-vert2").values()) == {"bike_path"}  # an Overture cycleway


def test_with_sfmta_data_overtures_bicycle_flag_no_longer_makes_a_lane():
    feats = road_features()
    for f in feats:
        if f["id"] == "s-side1":
            f["properties"]["bike_desig"] = 3  # a signed route, flagged by Overture
    assert set(_cats(_graph(feats), "s-side1").values()) == {"bike_lane"}  # no SFMTA data
    assert set(_cats(_graph(feats, []), "s-side1").values()) == {"shared"}  # SFMTA: none there


# --- profile D: boards keep out of traffic -----------------------------------

# A to B: 300 m straight down a busy street, or 500 m round by a park path.
DETOUR_NODES = {"A": (0, 0), "B": (300, 0), "C": (0, -100), "D": (300, -100)}


def _detour():
    feats = [
        _seg("street", "Busy Street", "residential", [(0, 0), (300, 0)], conns=[["conn-A", 0.0], ["conn-B", 1.0]]),
        _seg("p1", "Park Path", "cycleway", [(0, 0), (0, -100)], conns=[["conn-A", 0.0], ["conn-C", 1.0]]),
        _seg("p2", "Park Path", "cycleway", [(0, -100), (300, -100)], conns=[["conn-C", 0.0], ["conn-D", 1.0]]),
        _seg("p3", "Park Path", "cycleway", [(300, -100), (300, 0)], conns=[["conn-D", 0.0], ["conn-B", 1.0]]),
    ]
    return _graph(feats, nodes=DETOUR_NODES)


def test_boards_take_the_path_round_where_scooters_take_the_street():
    g = _detour()
    a, b = m_to_lonlat(0, 0), m_to_lonlat(300, 0)
    board = find_route(g, *a, *b, vehicle="emb")
    assert board.distance_m == pytest.approx(500, abs=2)
    assert board.traffic_m == pytest.approx(0, abs=0.1) and board.traffic_paths == []
    # EUCs (some now good for 60 mph) route like e-scooters
    euc = find_route(g, *a, *b, vehicle="euc")
    assert euc.distance_m == pytest.approx(300, abs=2)
    scooter = find_route(g, *a, *b, vehicle="scooter")
    assert scooter.distance_m == pytest.approx(300, abs=2)
    assert scooter.traffic_m == pytest.approx(300, abs=2) and len(scooter.traffic_paths) == 1


def test_profile_d_prices_traffic_above_every_bikeway():
    d = config.COST_PROFILES["D"]
    worst_bikeway = max(d["bike_path"], d["bike_lane"], d["living_street"])
    assert min(d[c] for c in config.MIXED_TRAFFIC) > 2 * worst_bikeway
    assert d["bike_path"] == min(d.values())


def test_traffic_stretches_are_merged_and_measured():
    g = RoadGraph()
    g.build({"type": "FeatureCollection", "features": road_features()}, {f"conn-{k}": list(m_to_lonlat(*p)) for k, p in NODES.items()})
    # A to F for a board: Main Street (shared, 200 m in two pieces) then the Cycle Path.
    r = find_route(g, *m_to_lonlat(0, 0), *m_to_lonlat(200, -100), vehicle="emb")
    assert r.traffic_m == pytest.approx(200, abs=2)
    assert len(r.traffic_paths) == 1  # the two Main Street blocks are one stretch
    assert r.traffic_paths[0][0] == pytest.approx(list(m_to_lonlat(0, 0)))
    assert r.traffic_paths[0][-1] == pytest.approx(list(m_to_lonlat(200, 0)))


def test_api_reports_traffic_for_boards(synth_data_dir):
    from fastapi.testclient import TestClient

    from pev_buddy.api import create_app

    def pt(dx, dy):
        lon, lat = m_to_lonlat(dx, dy)
        return {"lat": lat, "lon": lon}

    with TestClient(create_app(data_dir=str(synth_data_dir))) as c:
        board = c.post("/api/route", json={"start": pt(0, 0), "end": pt(200, -100), "vehicle": "emb"}).json()
        scooter = c.post("/api/route", json={"start": pt(0, 0), "end": pt(200, -100), "vehicle": "scooter"}).json()
        vehicles = {v["id"]: v for v in c.get("/api/vehicles").json()["vehicles"]}
    assert board["traffic_m"] == pytest.approx(200, abs=2) and len(board["traffic_paths"]) == 1
    assert "no bike path or lane for 0.1 mi: those stretches are with car traffic (red on the map)" in board["warnings"]
    assert not any("no bike path or lane" in w for w in scooter["warnings"])
    assert vehicles["emb"]["avoid_traffic"]
    assert not vehicles["scooter"]["avoid_traffic"] and not vehicles["euc"]["avoid_traffic"]
