from conftest import m_to_lonlat, road_features, make_road_graph
from pev_buddy.graph import RoadGraph
from pev_buddy.routing import find_route
from pev_buddy.turns import _classify, build_steps


def test_steps_for_simple_route(graph):
    a = m_to_lonlat(0, 0)
    f = m_to_lonlat(200, -100)
    r = find_route(graph, *a, *f)
    steps = build_steps(graph, r.edge_idxs, "My Destination")
    # depart east on Main, right turn at C onto the cycleway, arrive
    assert [s["maneuver"] for s in steps] == ["depart", "right", "arrive"]
    assert steps[0]["instruction"] == "Head east on Main Street."
    assert steps[1]["instruction"] == "Turn right onto Cycle Path."
    assert steps[2]["instruction"] == "Arrive at My Destination."
    # total distance across steps matches the route
    assert sum(s["distance_m"] for s in steps) == r.distance_m
    # turn point of the left turn is node C
    cx, cy = m_to_lonlat(200, 0)
    assert abs(steps[1]["turn_point"][0] - cx) < 1e-5


def test_steps_unnamed_road(graph):
    feats = road_features()
    for f in feats:
        if f["properties"]["id"] == "s-main2":
            f["properties"]["name"] = None
    g = make_road_graph(features=feats)
    a = m_to_lonlat(0, 0)
    f = m_to_lonlat(200, -100)
    r = find_route(g, *a, *f)
    steps = build_steps(g, r.edge_idxs, "D")
    # B->C is unnamed and not a gentle continuation of the named segment, so it
    # gets its own unnamed-road step; nothing may render as "onto ." etc.
    assert any("the street" in s["instruction"] or "the road" in s["instruction"] for s in steps)
    for s in steps:
        assert "None" not in s["instruction"]
        assert " ." not in s["instruction"]


def test_signpost_label_enriches_turn(graph):
    # signpost on Cross Street (s-vert1): turning onto Side Street at D shows "Downtown"
    feats = road_features()
    for f in feats:
        if f["properties"]["id"] == "s-vert1":
            f["properties"]["dest"] = [["s-side1", "conn-D", "Downtown", 3]]
    g = make_road_graph(features=feats)
    a = m_to_lonlat(0, 0)
    e = m_to_lonlat(100, -100)
    r = find_route(g, *a, *e)
    ids = [g.segments[seg]["id"] for seg in [g.edges[i][2] for i in r.edge_idxs]]
    assert "s-vert1" in ids and "s-side1" in ids
    steps = build_steps(g, r.edge_idxs, "D")
    assert any("Downtown" in s["instruction"] for s in steps)


def test_short_stub_after_turn_keeps_turn():
    # Overture splits streets at every crossing, so the first piece of a new
    # street can be a short unnamed stub. The turn onto that street must not be
    # swallowed by the short-step merge (regression: Cayuga Ave -> Cafe Encore
    # route lost every intermediate turn).
    from conftest import _seg
    from pev_buddy.graph import RoadGraph

    feats = [
        _seg("s-t-main", "Main Street", "residential", [(0, 0), (100, 0)], conns=[["c-a", 0.0], ["c-b", 1.0]]),
        _seg("s-t-stub", None, "residential", [(100, 0), (100, -13)], conns=[["c-b", 0.0], ["c-c", 1.0]]),
        _seg("s-t-cross", "Cross Street", "residential", [(100, -13), (100, -100)], conns=[["c-c", 0.0], ["c-d", 1.0]]),
    ]
    connectors = {
        "c-a": list(m_to_lonlat(0, 0)),
        "c-b": list(m_to_lonlat(100, 0)),
        "c-c": list(m_to_lonlat(100, -13)),
        "c-d": list(m_to_lonlat(100, -100)),
    }
    g = RoadGraph()
    g.build({"type": "FeatureCollection", "features": feats}, connectors)
    r = find_route(g, *m_to_lonlat(0, 0), *m_to_lonlat(100, -100))
    steps = build_steps(g, r.edge_idxs, "Home")
    assert [s["maneuver"] for s in steps] == ["depart", "right", "arrive"]
    assert steps[0]["instruction"] == "Head east on Main Street."
    assert steps[1]["instruction"] == "Turn right onto Cross Street."
    # the zero-length arrival carries the street's name adopted from the stub
    assert steps[2]["instruction"] == "Arrive at Home."
    assert steps[2]["road"] == "Cross Street"
    assert sum(s["distance_m"] for s in steps) == r.distance_m


def test_classify_sign_and_buckets():
    # Straight / continue boundary
    assert _classify(0.0)[0] == "straight"
    assert _classify(25.0)[0] == "straight"
    # Positive angle = right turn, negative = left (geo.turn_angle convention).
    assert _classify(30.0) == ("slight_right", "Turn slightly right onto")
    assert _classify(-30.0) == ("slight_left", "Turn slightly left onto")
    assert _classify(90.0)[0] == "right"
    assert _classify(-90.0)[0] == "left"
    # A hard 120 deg turn is a full turn, not a U-turn
    assert _classify(120.0)[0] == "right"
    assert _classify(-120.0)[0] == "left"
    assert _classify(150.0)[0] == "uturn"
    assert _classify(-170.0)[0] == "uturn"


def test_short_piece_merges_into_previous_step():
    # A 30 m side street between two longer roads is shorter than MIN_STEP_M,
    # so it folds into the previous step instead of producing its own turn.
    from conftest import _seg

    feats = [
        _seg("s-m1", "Main Street", "residential", [(0, 0), (100, 0)], conns=[["c-a", 0.0], ["c-b", 1.0]]),
        _seg("s-m2", "Main Street", "residential", [(100, 0), (200, 0)], conns=[["c-b", 0.0], ["c-c", 1.0]]),
        _seg("s-tiny", "Tiny St", "residential", [(200, 0), (200, -30)], conns=[["c-c", 0.0], ["c-d", 1.0]]),
        _seg("s-goal", "Cycle Path", "cycleway", [(200, -30), (200, -100)], conns=[["c-d", 0.0], ["c-e", 1.0]]),
    ]
    connectors = {
        "c-a": list(m_to_lonlat(0, 0)),
        "c-b": list(m_to_lonlat(100, 0)),
        "c-c": list(m_to_lonlat(200, 0)),
        "c-d": list(m_to_lonlat(200, -30)),
        "c-e": list(m_to_lonlat(200, -100)),
    }
    g = RoadGraph()
    g.build({"type": "FeatureCollection", "features": feats}, connectors)
    r = find_route(g, *m_to_lonlat(0, 0), *m_to_lonlat(200, -100))
    steps = build_steps(g, r.edge_idxs, "Home")
    assert [s["maneuver"] for s in steps] == ["depart", "arrive"]
    assert not any("Tiny" in s["instruction"] for s in steps)
    assert sum(s["distance_m"] for s in steps) == r.distance_m
    # step geometries stay contiguous across the merged stub
    d = m_to_lonlat(200, -30)
    assert abs(steps[0]["geometry"][-1][0] - d[0]) < 1e-5
    assert abs(steps[1]["geometry"][0][0] - d[0]) < 1e-5


def test_depart_adopts_name_from_named_continuation():
    # A route that starts on a short unnamed stub continuing straight into a
    # named street: the departure instruction uses the street's name.
    from conftest import _seg

    feats = [
        _seg("s-stub", None, "residential", [(0, 0), (13, 0)], conns=[["c-a", 0.0], ["c-b", 1.0]]),
        _seg("s-main", "Main Street", "residential", [(13, 0), (100, 0)], conns=[["c-b", 0.0], ["c-c", 1.0]]),
    ]
    connectors = {
        "c-a": list(m_to_lonlat(0, 0)),
        "c-b": list(m_to_lonlat(13, 0)),
        "c-c": list(m_to_lonlat(100, 0)),
    }
    g = RoadGraph()
    g.build({"type": "FeatureCollection", "features": feats}, connectors)
    r = find_route(g, *m_to_lonlat(0, 0), *m_to_lonlat(100, 0))
    steps = build_steps(g, r.edge_idxs, "Home")
    assert len(steps) == 1
    assert steps[0]["maneuver"] == "depart"
    assert steps[0]["instruction"] == "Head east on Main Street."


def test_same_name_bend_does_not_repeat_onto():
    # A curved street Overture splits into segments keeps its name; the bend
    # must read as "staying on", not "turn onto" the same street.
    from conftest import _seg
    from pev_buddy.graph import RoadGraph

    feats = [
        _seg("s-c1", "Curve Street", "residential", [(0, 0), (100, 0)], conns=[["c-p", 0.0], ["c-q", 1.0]]),
        _seg("s-c2", "Curve Street", "residential", [(100, 0), (100, 100)], conns=[["c-q", 0.0], ["c-r", 1.0]]),
    ]
    connectors = {
        "c-p": list(m_to_lonlat(0, 0)),
        "c-q": list(m_to_lonlat(100, 0)),
        "c-r": list(m_to_lonlat(100, 100)),
    }
    g = RoadGraph()
    g.build({"type": "FeatureCollection", "features": feats}, connectors)
    r = find_route(g, *m_to_lonlat(0, 0), *m_to_lonlat(100, 100))
    steps = build_steps(g, r.edge_idxs, "Home")
    assert [s["maneuver"] for s in steps] == ["depart", "left", "arrive"]
    assert steps[1]["instruction"] == "Turn left, staying on Curve Street."


def test_empty_route_step(graph):
    steps = build_steps(graph, [], "Station X")
    assert len(steps) == 1
    assert steps[0]["maneuver"] == "arrive"
    assert steps[0]["instruction"] == "Arrive at Station X."
    assert steps[0]["geometry"] is None


def test_step_geometry_covers_route(graph):
    a = m_to_lonlat(0, 0)
    f = m_to_lonlat(200, -100)
    r = find_route(graph, *a, *f)
    steps = build_steps(graph, r.edge_idxs, "D")
    assert all(s["geometry"] for s in steps)
    # first step starts at the route's start
    first = steps[0]["geometry"][0]
    assert abs(first[0] - r.path[0][0]) < 1e-4 and abs(first[1] - r.path[0][1]) < 1e-4
    # arrival step is a single point at the route's end
    last = steps[-1]
    assert last["maneuver"] == "arrive"
    assert len(last["geometry"]) == 1
    assert abs(last["geometry"][0][0] - r.path[-1][0]) < 1e-4
    assert abs(last["geometry"][0][1] - r.path[-1][1]) < 1e-4
