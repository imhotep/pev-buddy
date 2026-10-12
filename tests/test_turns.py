import pytest

from conftest import _seg, m_to_lonlat, road_features, make_road_graph
from pev_buddy.graph import RoadGraph
from pev_buddy.routing import find_route
from pev_buddy.turns import _classify, _sentence, build_steps


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
    # B->C is a long unnamed street straight on from Main Street: it keeps its
    # own step, worded without inventing a name ("onto the street", "onto .").
    assert [s["instruction"] for s in steps[:2]] == ["Head east on Main Street.", "Continue."]
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
    assert _classify(30.0) == ("slight_right", "Turn slightly right")
    assert _classify(-30.0) == ("slight_left", "Turn slightly left")
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


# --- unnamed segments --------------------------------------------------------
# Overture leaves many bike-lane pieces and short connectors without a name;
# they must not fragment a street into "Turn onto the street" steps.


def _chain_graph(*roads):
    """A RoadGraph from (name, class, [(x_m, y_m), ...]) polylines, each
    starting where the previous one ends."""
    feats, connectors = [], {}
    for n, (name, cls, pts) in enumerate(roads):
        ends = []
        for x, y in (pts[0], pts[-1]):
            cid = f"c-{x}-{y}"
            connectors[cid] = list(m_to_lonlat(x, y))
            ends.append(cid)
        feats.append(_seg(f"s-{n}", name, cls, pts, conns=[[ends[0], 0.0], [ends[1], 1.0]]))
    g = RoadGraph()
    g.build({"type": "FeatureCollection", "features": feats}, connectors)
    return g


def _steps_along(g, start, end, dest="Home"):
    r = find_route(g, *m_to_lonlat(*start), *m_to_lonlat(*end))
    steps = build_steps(g, r.edge_idxs, dest)
    assert sum(s["distance_m"] for s in steps) == pytest.approx(r.distance_m)
    for s in steps:
        assert "the street" not in s["instruction"] and "the road" not in s["instruction"]
    return steps


def test_unnamed_run_between_same_street_is_absorbed():
    # Howard Street jogs onto an unnamed 150 m bike-lane piece and back with
    # no real turn either way: one Howard Street step, not three.
    g = _chain_graph(
        ("Howard Street", "residential", [(0, 0), (100, 0)]),
        (None, "cycleway", [(100, 0), (120, -10), (230, -10), (250, 0)]),
        ("Howard Street", "residential", [(250, 0), (400, 0)]),
        ("Oak Street", "residential", [(400, 0), (400, -100)]),
    )
    steps = _steps_along(g, (0, 0), (400, -100))
    assert [s["instruction"] for s in steps] == [
        "Head east on Howard Street.",
        "Turn right onto Oak Street.",
        "Arrive at Home.",
    ]
    assert steps[0]["distance_m"] > 400  # the whole jog belongs to Howard Street


def test_unnamed_detour_with_real_turns_is_not_absorbed():
    # Same street on both sides, but the unnamed piece is entered and left
    # with full turns: real maneuvers, not a bike-lane jog.
    g = _chain_graph(
        ("Howard Street", "residential", [(0, 0), (100, 0)]),
        (None, "cycleway", [(100, 0), (100, -100), (200, -100), (200, 0)]),
        ("Howard Street", "residential", [(200, 0), (300, 0)]),
    )
    steps = _steps_along(g, (0, 0), (300, 0))
    assert steps[1]["instruction"] == "Turn right onto the bike path."
    # Back onto Howard Street from the bike path: "onto", not "staying on".
    assert steps[2]["instruction"] == "Turn right onto Howard Street."


def test_short_unnamed_connector_gets_no_step():
    # A 50 m unnamed connector between two streets (longer than MIN_STEP_M):
    # the rider hears about Oak Street, not about the connector.
    g = _chain_graph(
        ("Main Street", "residential", [(0, 0), (100, 0)]),
        (None, "residential", [(100, 0), (100, -50)]),
        ("Oak Street", "residential", [(100, -50), (200, -50)]),
    )
    steps = _steps_along(g, (0, 0), (200, -50))
    assert [s["instruction"] for s in steps] == [
        "Head east on Main Street.",
        "Turn left onto Oak Street.",
        "Arrive at Home.",
    ]
    assert steps[0]["distance_m"] == pytest.approx(150, abs=1)


def test_route_starting_on_short_unnamed_piece_departs_on_the_named_road():
    g = _chain_graph(
        (None, "footway", [(0, 0), (0, -15)]),
        ("Main Street", "residential", [(0, -15), (100, -15)]),
        ("Oak Street", "residential", [(100, -15), (100, -100)]),
    )
    steps = _steps_along(g, (0, 0), (100, -100))
    assert steps[0]["instruction"] == "Head east on Main Street."
    assert steps[1]["instruction"] == "Turn right onto Oak Street."


def test_unnamed_ways_are_described_by_their_class():
    g = _chain_graph(
        ("Main Street", "residential", [(0, 0), (100, 0)]),
        (None, "cycleway", [(100, 0), (100, -150)]),
        (None, "footway", [(100, -150), (200, -150)]),
    )
    steps = _steps_along(g, (0, 0), (200, -150))
    assert [s["instruction"] for s in steps] == [
        "Head east on Main Street.",
        "Turn right onto the bike path.",
        "Turn left onto the sidewalk.",
        "Arrive at Home.",
    ]
    assert all(s["road"] is None for s in steps[1:])


def test_turn_onto_unnamed_street_names_no_road():
    g = _chain_graph(
        ("Main Street", "residential", [(0, 0), (100, 0)]),
        (None, "service", [(100, 0), (100, 100)]),
    )
    steps = _steps_along(g, (0, 0), (100, 100))
    assert steps[1]["instruction"] == "Turn left."


def test_staying_on_only_when_already_on_that_street():
    # A 20 m Howard Street stub folds into the Main Street step, so the turn
    # after it is onto Howard Street, not "staying on" it.
    g = _chain_graph(
        ("Main Street", "residential", [(0, 0), (100, 0)]),
        ("Howard Street", "residential", [(100, 0), (100, -20)]),
        ("Howard Street", "residential", [(100, -20), (200, -20)]),
    )
    steps = _steps_along(g, (0, 0), (200, -20))
    assert steps[1]["instruction"] == "Turn left onto Howard Street."


def test_no_double_punctuation_after_names_ending_in_punctuation():
    # "Herb Caen Way..." is a real SF street name.
    g = _chain_graph(
        ("Herb Caen Way...", "residential", [(0, 0), (100, 0)]),
        ("Pier Way…", "residential", [(100, 0), (100, -100)]),
    )
    steps = _steps_along(g, (0, 0), (100, -100), dest="Pier 39!")
    assert [s["instruction"] for s in steps] == [
        "Head east on Herb Caen Way...",
        "Turn right onto Pier Way…",
        "Arrive at Pier 39!",
    ]
    assert _sentence("Turn left") == "Turn left."
    assert _sentence("Really?") == "Really?"
