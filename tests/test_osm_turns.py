"""Bicycle-exempt turn restrictions: OSM's `except=bicycle`, which Overture drops."""

import json

import pytest

from conftest import NODES, _seg, m_to_lonlat, road_features
from pev_buddy import osm_turns, routing
from pev_buddy.graph import RoadGraph

# Overpass answer for Mission St at 16th (relation 8824406, trimmed).
PAYLOAD = {
    "elements": [
        {
            "type": "relation",
            "id": 8824406,
            "tags": {"type": "restriction", "restriction": "only_right_turn", "except": "bicycle;bus;goods;hgv;taxi"},
            "members": [
                {"type": "way", "ref": 1, "role": "from"},
                {"type": "node", "ref": 9, "role": "via"},
                {"type": "way", "ref": 2, "role": "to"},
            ],
        },
        # A psv-only exemption: not ours to lift.
        {
            "type": "relation",
            "id": 6504868,
            "tags": {"type": "restriction", "restriction": "no_left_turn", "except": "psv"},
            "members": [{"type": "node", "ref": 9, "role": "via"}],
        },
        {"type": "way", "id": 1, "tags": {"name": "Mission Street"}},
        {"type": "way", "id": 2, "tags": {"name": "16th Street"}},
        {"type": "node", "id": 9, "lon": -122.4195, "lat": 37.765},
    ]
}


def test_parse_keeps_bicycle_exemptions_only():
    assert osm_turns.parse(PAYLOAD) == [
        {"id": 8824406, "restriction": "only_right_turn", "via": [-122.4195, 37.765], "from": ["Mission Street"], "to": ["16th Street"]}
    ]


def test_extract_falls_back_across_mirrors(tmp_path):
    tried = []

    def post(url, query):
        tried.append(url)
        if len(tried) == 1:
            raise TimeoutError("busy")
        return PAYLOAD

    assert osm_turns.extract((-122.52, 37.70, -122.35, 37.84), tmp_path / "t.json", post=post) == 1
    assert len(tried) == 2 and "except" in osm_turns._query((0, 0, 1, 1))
    assert json.loads((tmp_path / "t.json").read_text())[0]["id"] == 8824406


def test_extract_raises_when_every_mirror_fails(tmp_path):
    def post(url, query):
        raise TimeoutError("busy")

    with pytest.raises(RuntimeError):
        osm_turns.extract((0, 0, 1, 1), tmp_path / "t.json", post=post)
    assert not (tmp_path / "t.json").exists()


def _graph(exempt=None):
    # Main Street at B: "only right turn" for cars, which Overture renders as
    # a ban on going straight on to C.
    feats = road_features()
    feats[0] = _seg(
        "s-main1", "Main Street", "residential", [(0, 0), (100, 0)],
        prohib=[["conn-B", "s-main2", 1]], conns=[["conn-A", 0.0], ["conn-B", 1.0]],
    )
    connectors = {f"conn-{n}": list(m_to_lonlat(*p)) for n, p in NODES.items()}
    g = RoadGraph()
    g.build({"type": "FeatureCollection", "features": feats}, connectors, exempt)
    return g


def _route(g, a, b, vehicle):
    (alon, alat), (blon, blat) = m_to_lonlat(*NODES[a]), m_to_lonlat(*NODES[b])
    r = routing.find_route(g, alon, alat, blon, blat, vehicle=vehicle)
    names = {i: cid.removeprefix("conn-") for cid, i in g.node_index.items()}
    return [names[r.start_node]] + [names[g.edges[e][1]] for e in r.edge_idxs]


def test_without_osm_data_the_ban_binds_everyone():
    g = _graph()
    assert _route(g, "A", "C", "scooter") != ["A", "B", "C"]


def test_bicycle_exempt_ban_binds_mopeds_only(tmp_path):
    rule = {"id": 1, "restriction": "only_right_turn", "via": list(m_to_lonlat(100, 0)), "from": ["Main Street"], "to": ["Other"]}
    g = _graph([rule])
    assert _route(g, "A", "C", "scooter") == ["A", "B", "C"]
    assert _route(g, "A", "C", "ebike_c3") == ["A", "B", "C"]
    assert _route(g, "A", "C", "moped") != ["A", "B", "C"]
    # ...and it survives the runtime bundle.
    g.save_bundle(tmp_path)
    g2 = RoadGraph.load_bundle(tmp_path)
    assert _route(g2, "A", "C", "scooter") == ["A", "B", "C"]
    assert _route(g2, "A", "C", "moped") != ["A", "B", "C"]


def test_rule_elsewhere_or_on_another_street_lifts_nothing():
    far = {"id": 1, "restriction": "only_right_turn", "via": list(m_to_lonlat(100, 200)), "from": ["Main Street"], "to": []}
    other = {"id": 2, "restriction": "only_right_turn", "via": list(m_to_lonlat(100, 0)), "from": ["Side Street"], "to": []}
    g = _graph([far, other])
    assert not g.prohib_moto
    assert _route(g, "A", "C", "scooter") != ["A", "B", "C"]


def test_no_turn_rule_lifts_only_its_to_street():
    rule = {"id": 1, "restriction": "no_straight_on", "via": list(m_to_lonlat(100, 0)), "from": ["Main Street"], "to": ["Elsewhere"]}
    g = _graph([rule])
    assert not g.prohib_moto  # the banned move goes to Main Street, not Elsewhere
    rule["to"] = ["Main Street"]
    assert _graph([rule]).prohib_moto
