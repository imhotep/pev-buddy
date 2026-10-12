"""Hill-aware routing: elevation tiles, climb costs, uphill ETAs, and the
street-speed inheritance that keeps unposted blocks legal for e-scooters."""

import io

import numpy as np
import pytest

from conftest import NODES, _seg, m_to_lonlat, make_road_graph, road_features
from pev_buddy import config, elevation, routing
from pev_buddy.graph import RoadGraph


def _terrarium_png(meters: float) -> bytes:
    from PIL import Image

    v = meters + 32768.0
    r, g = divmod(int(v), 256)
    b = int(round((v - int(v)) * 256))
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (r, g, b)).save(buf, format="PNG")
    return buf.getvalue()


def test_terrarium_decode():
    rgb = np.array([[[128, 0, 0], [128, 100, 128], [127, 255, 0]]], dtype=np.uint8)
    assert elevation.decode_terrarium(rgb).tolist() == [[0.0, 100.5, -1.0]]


def test_fetch_grid_mosaics_tiles_and_samples_points():
    fetched = []

    def fetch(url):
        fetched.append(url)
        return _terrarium_png(42.0)

    lon, lat = m_to_lonlat(100, -100)
    grid = elevation.fetch_grid((lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01), fetch=fetch)
    assert fetched and all(u.startswith("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/14/") for u in fetched)
    assert elevation.sample(grid, [[lon, lat]]).tolist() == [42.0]
    # Far outside the mosaic: unknown, not a made-up height.
    assert np.isnan(elevation.sample(grid, [[lon + 1.0, lat]])[0])


def _with_elevation(g: RoadGraph, heights: dict[str, float]) -> RoadGraph:
    elev = np.zeros(len(g.node_coords))
    for name, h in heights.items():
        elev[g.node_index[f"conn-{name}"]] = h
    g.node_elev = elev
    g._hill_cache = {}
    return g


def _route_nodes(g, r):
    names = {i: cid.removeprefix("conn-") for cid, i in g.node_index.items()}
    return [names[r.start_node]] + [names[g.edges[e][1]] for e in r.edge_idxs]


def _route(g, a, b, vehicle="scooter"):
    (alon, alat), (blon, blat) = m_to_lonlat(*NODES[a]), m_to_lonlat(*NODES[b])
    return routing.find_route(g, alon, alat, blon, blat, vehicle=vehicle)


def test_flat_network_keeps_the_shortest_route():
    g = _with_elevation(make_road_graph(), {})
    assert _route_nodes(g, _route(g, "A", "F")) == ["A", "B", "C", "F"]


def test_route_goes_around_a_steep_hill():
    # B sits 20 m above A: a 20% climb on the shortest route. The flat way
    # round (A-D-E-F) is only 15 m longer in cost terms.
    g = _with_elevation(make_road_graph(), {"B": 20.0, "C": 20.0})
    r = _route(g, "A", "F")
    assert _route_nodes(g, r) == ["A", "D", "E", "F"]
    assert r.climb_m == 0.0


def test_moped_shrugs_off_a_gentle_hill_a_scooter_avoids():
    # 6 m over 100 m (6%): worth a detour on a 15 mph scooter, not on a moped.
    g = _with_elevation(make_road_graph(), {"B": 6.0, "C": 6.0})
    assert _route_nodes(g, _route(g, "A", "F", "scooter")) == ["A", "D", "E", "F"]
    climb_scooter = config.hill_cost("A", 100.0, 6.0)
    climb_moped = config.hill_cost("C", 100.0, 6.0)
    assert climb_moped < climb_scooter / 5


def test_hill_cost_shape():
    assert config.hill_cost("A", 100.0, 0.0) == 0.0
    assert config.hill_cost("A", 100.0, -5.0) == 0.0  # gentle descent: free
    assert config.hill_cost("A", 100.0, -25.0) > 0.0  # 25% descent: braking on small wheels
    gentle, steep = config.hill_cost("A", 100.0, 5.0), config.hill_cost("A", 100.0, 20.0)
    assert steep > 4 * gentle  # past STEEP_GRADE each meter weighs more
    assert np.allclose(config.hill_cost("A", [100.0, 100.0], [5.0, 20.0]), [gentle, steep])


def test_uphill_is_slower_and_reported_as_climb():
    flat = _with_elevation(make_road_graph(), {})
    hilly = _with_elevation(make_road_graph(), {"D": 10.0, "E": 10.0, "F": 10.0, "B": 30.0, "C": 30.0})
    r_flat = _route(flat, "A", "D")
    r_up = _route(hilly, "A", "D")
    assert r_up.edge_idxs == r_flat.edge_idxs
    assert r_up.climb_m == pytest.approx(10.0)
    assert r_up.duration_s > r_flat.duration_s * 1.3
    # Coming back down is not slowed.
    assert _route(hilly, "D", "A").duration_s == pytest.approx(_route(flat, "D", "A").duration_s)


def test_elevation_survives_the_bundle(tmp_path):
    g = _with_elevation(make_road_graph(), {"B": 12.5})
    g.save_bundle(tmp_path)
    g2 = RoadGraph.load_bundle(tmp_path)
    assert g2.node_elev[g2.node_index["conn-B"]] == pytest.approx(12.5)
    assert _route_nodes(g2, _route(g2, "A", "F")) == _route_nodes(g, _route(g, "A", "F"))


def test_bundle_without_elevation_routes_hill_blind(tmp_path):
    g = make_road_graph()
    assert np.isnan(g.node_elev).all()
    assert _route_nodes(g, _route(g, "A", "F")) == ["A", "B", "C", "F"]


def test_set_elevation_floors_bathymetry_at_sea_level():
    g = make_road_graph()
    lon, lat = m_to_lonlat(0, 0)

    def fetch(url):
        return _terrarium_png(-30.0)  # a pier's seabed

    g.set_elevation(elevation.fetch_grid((lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01), fetch=fetch))
    assert np.nanmin(g.node_elev) == 0.0


def test_unposted_block_inherits_its_streets_limit():
    # Long Street: posted 25 mph on its first block, nothing on the next. The
    # secondary-class default (40 mph) would bar e-scooters from block two.
    feats = road_features() + [
        _seg("s-long1", "Long Street", "secondary", [(0, 0), (0, 100)], speed=25, conns=[["conn-A", 0.0], ["conn-K", 1.0]]),
        _seg("s-long2", "Long Street", "secondary", [(0, 100), (0, 200)], conns=[["conn-K", 0.0], ["conn-L", 1.0]]),
        # Same street name, different class: not the same road; keeps the default.
        _seg("s-long3", "Long Street", "tertiary", [(0, 200), (0, 300)], conns=[["conn-L", 0.0], ["conn-M", 1.0]]),
    ]
    extra = {"K": (0, 100), "L": (0, 200), "M": (0, 300)}
    from pev_buddy.graph import RoadGraph as RG

    connectors = {f"conn-{n}": list(m_to_lonlat(*p)) for n, p in {**NODES, **extra}.items()}
    g = RG()
    g.build({"type": "FeatureCollection", "features": feats}, connectors)
    by_seg = {g.segments[e[2]]["id"]: (i, e) for i, e in enumerate(g.edges)}
    i2, e2 = by_seg["s-long2"]
    assert e2[6] == 25  # effective limit inherited
    assert e2[7] is None  # posted limit is still what the data says
    assert g.edge_ok["scooter"][i2] == 1
    _i3, e3 = by_seg["s-long3"]
    assert e3[6] == config.SPEED_BY_CLASS_DEFAULT_MPH["tertiary"]
