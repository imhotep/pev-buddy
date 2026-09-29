import pytest

from conftest import m_to_lonlat, road_features, make_road_graph
from pev_buddy.routing import RouteError, SnapError, find_route


def seg_ids(graph, r):
    return [graph.segments[e[2]]["id"] for e in map(graph.edges.__getitem__, r.edge_idxs)]


def test_route_prefers_cycleway(graph):
    a = m_to_lonlat(0, 0)
    f = m_to_lonlat(200, -100)
    r = find_route(graph, *a, *f, vehicle="scooter")
    # physically 3 x 100 m; the cycleway's cost discount makes A->B->C->F beat
    # the all-residential A->D->E->F even though both are ~300 m
    assert 295 < r.distance_m < 302
    last_seg = graph.segments[graph.edges[r.edge_idxs[-1]][2]]
    assert last_seg["class"] == "cycleway"
    ids = seg_ids(graph, r)
    assert "s-vert2" in ids


def test_route_respects_one_way(graph):
    # H2 -> G: must go H2->D (slow boulevard) -> G (one-way alley forward).
    # The union graph also holds the alley's backward heading, but its
    # bicycle-only access mask keeps e-bikes (and scooters) going forward.
    h2 = m_to_lonlat(-100, -100)
    g = m_to_lonlat(0, -200)
    for vehicle in ("scooter", "ebike_c1"):
        r = find_route(graph, *h2, *g, vehicle=vehicle)
        ids = seg_ids(graph, r)
        assert "s-slow" in ids and "s-oneway" in ids
        for e in r.edge_idxs:
            if graph.segments[graph.edges[e][2]]["id"] == "s-oneway":
                assert graph.edges[e][3] == 0, "one-way alley must be traversed forward only"


def test_no_immediate_uturn_in_any_route(graph):
    for s, e in [
        (m_to_lonlat(0, 0), m_to_lonlat(200, -100)),
        (m_to_lonlat(200, 0), m_to_lonlat(0, -200)),
        (m_to_lonlat(-100, -100), m_to_lonlat(250, -100)),
        (m_to_lonlat(200, -100), m_to_lonlat(0, 0)),
        (m_to_lonlat(250, -100), m_to_lonlat(0, 0)),
    ]:
        r = find_route(graph, *s, *e, vehicle="scooter")
        for i in range(1, len(r.edge_idxs)):
            prev = graph.edges[r.edge_idxs[i - 1]]
            cur = graph.edges[r.edge_idxs[i]]
            assert not (prev[2] == cur[2] and prev[3] != cur[3]), "immediate U-turn on same segment"


def test_snap_error_when_far_from_network(graph):
    with pytest.raises(SnapError):
        find_route(graph, *m_to_lonlat(0, 0), *m_to_lonlat(50000, 50000), vehicle="scooter")


def test_route_to_arterial_junction(graph):
    # node H sits only on the 40 mph arterial s-fast; an e-bike (no road-speed
    # restriction) can take it, an e-scooter (roads <= 25 mph) cannot.
    a = m_to_lonlat(0, 0)
    h = m_to_lonlat(-100, 0)
    r = find_route(graph, *a, *h, vehicle="ebike_c1")
    ids = seg_ids(graph, r)
    assert "s-fast" in ids
    assert "arterial" in r.segment_stats
    with pytest.raises(RouteError):
        find_route(graph, *a, *h, vehicle="scooter", snap_radius_m=50)


def test_route_error_when_disconnected(graph):
    # node I is only reachable via the excluded motorway s-hwy; with a tight
    # snap radius no usable junction is close enough, so the destination stays
    # on I and the search must fail.
    i = m_to_lonlat(-100, 100)
    a = m_to_lonlat(0, 0)
    with pytest.raises(RouteError):
        find_route(graph, *a, *i, vehicle="scooter", snap_radius_m=50)


def test_snap_prefers_usable_junction(graph):
    from pev_buddy.routing import snap_to_node

    # 20 m south of dead-end node I (which only touches the excluded motorway).
    p = m_to_lonlat(-100, 80)
    # an e-bike may use the 40 mph arterial, so H is the usable junction (80 m)
    node, dist = snap_to_node(graph, p[0], p[1], vehicle="ebike_c1", prefer="end")
    assert node == graph.node_index["conn-H"]
    assert 79 < dist < 81
    # an e-scooter may not: H sits only on the 40 mph road, so it snaps to the
    # nearest junction the scooter may actually ride (A, ~128 m)
    node, dist = snap_to_node(graph, p[0], p[1], vehicle="scooter", prefer="end")
    assert node == graph.node_index["conn-A"]
    assert 127 < dist < 129


def test_emoto_is_blocked(graph):
    a = m_to_lonlat(0, 0)
    f = m_to_lonlat(200, -100)
    with pytest.raises(RouteError, match="not street-legal"):
        find_route(graph, *a, *f, vehicle="emoto")


def test_unknown_vehicle_rejected(graph):
    a = m_to_lonlat(0, 0)
    f = m_to_lonlat(200, -100)
    with pytest.raises(ValueError):
        find_route(graph, *a, *f, vehicle="Q")


def test_prohibited_transition_avoids_banned_road():
    feats = road_features()
    for f in feats:
        if f["properties"]["id"] == "s-main1":
            # arriving on s-main1, do not take s-main2 at connector B
            f["properties"]["prohib"] = [["conn-B", "s-main2", 3]]
    g = make_road_graph(features=feats)
    a = m_to_lonlat(0, 0)
    f = m_to_lonlat(200, -100)
    r = find_route(g, *a, *f, vehicle="scooter")
    ids = seg_ids(g, r)
    assert "s-main2" not in ids, "prohibited transition was not respected"
    # fallback path A->D->E->F
    assert ids.count("s-vert1") == 1 and ids.count("s-side1") == 1 and ids.count("s-side2") == 1


def test_start_equals_end(graph):
    a = m_to_lonlat(0, 0)
    r = find_route(graph, *a, *a, vehicle="scooter")
    assert r.distance_m == 0.0
    assert r.edge_idxs == []
