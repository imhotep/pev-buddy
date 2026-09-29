import math

from pev_buddy.graph import RoadGraph


def test_node_and_edge_counts(graph: RoadGraph):
    # A B C D E F G H H2 X Y I J
    assert graph.node_count == 13
    by_seg = {}
    for a, b, seg_idx, heading, *_ in graph.edges:
        by_seg.setdefault(seg_idx, []).append((a, b, heading))
    seg_by_name = {s["id"]: idx for idx, s in enumerate(graph.segments)}
    # the union graph holds both alley headings: the bicycle mask allows only
    # forward, the motorcycle mask (moped) allows both
    assert len(by_seg[seg_by_name["s-oneway"]]) == 2
    # 40 mph secondary is an arterial: in the union, both ways
    assert len(by_seg[seg_by_name["s-fast"]]) == 2
    # 20 mph secondary allowed both ways
    assert len(by_seg[seg_by_name["s-slow"]]) == 2
    # steps are never routed
    assert seg_by_name["s-steps"] not in by_seg, "steps must be excluded"
    # motorway (65 mph default) is never routed
    assert seg_by_name["s-hwy"] not in by_seg, "motorway must be excluded"
    # sidewalk is a high-cost connector, not excluded
    assert len(by_seg[seg_by_name["s-walk"]]) == 2
    # living street both ways
    assert len(by_seg[seg_by_name["s-living"]]) == 2
    # cycleway both ways
    assert len(by_seg[seg_by_name["s-vert2"]]) == 2


def test_every_edge_is_pev_compliant(graph: RoadGraph):
    from pev_buddy import config

    for _a, _b, _seg_idx, _heading, length, category, eff_speed, _posted in graph.edges:
        assert length > 0
        assert category in config.CATEGORIES
        if category in ("quiet", "shared", "arterial"):
            # nothing above the hard speed ceiling is in the graph, any vehicle
            assert eff_speed <= config.MAX_SPEED_MPH


def test_edge_geometry_is_continuous(graph: RoadGraph):
    for e in range(graph.edge_count):
        geom = graph.edge_geometry(e)
        assert len(geom) >= 2, f"edge {e} has no geometry"
        a, b = graph.edges[e][:2]
        sa, sb = graph.node(a), graph.node(b)
        assert math.hypot(geom[0][0] - sa[0], geom[0][1] - sa[1]) < 1e-6
        assert math.hypot(geom[-1][0] - sb[0], geom[-1][1] - sb[1]) < 1e-6
