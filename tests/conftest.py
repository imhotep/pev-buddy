"""Shared fixtures: a small synthetic road network for deterministic tests."""

import math

import pytest

ORIGIN = (-122.40, 37.78)


def m_to_lonlat(dx_m: float, dy_m: float) -> tuple[float, float]:
    lat = ORIGIN[1] + dy_m / 111320.0
    lon = ORIGIN[0] + dx_m / (111320.0 * math.cos(math.radians(ORIGIN[1])))
    return lon, lat


# Node layout (meters relative to ORIGIN):
#
#   I(-100,100)
#   H(-100,0)  A(0,0) --- B(100,0) --- C(200,0)
#                 |                |     |
#                 D(0,-100) --- E(100,-100) --- F(200,-100) --- X(250,-100) --- Y(300,-100)
#                 |
#                 G(0,-200)          H2(-100,-100)
#                                     |
#                                     J(-100,-200)
#
# H only touches a 40 mph arterial (s-fast) and an excluded motorway (s-hwy,
# to I). X is reachable only via excluded stairs (s-steps). Y only via a
# sidewalk (s-walk). J only via a living street (s-living).
NODES = {
    "A": (0, 0),
    "B": (100, 0),
    "C": (200, 0),
    "D": (0, -100),
    "E": (100, -100),
    "F": (200, -100),
    "G": (0, -200),
    "H": (-100, 0),
    "H2": (-100, -100),
    "X": (250, -100),
    "Y": (300, -100),
    "I": (-100, 100),
    "J": (-100, -200),
}


def _seg(sid, name, cls, points, **props):
    p = {
        "id": sid,
        "name": name,
        "class": cls,
        "speed": props.get("speed"),
        "ow": props.get("ow", 3),
        "ow_moto": props.get("ow_moto", 3),
        "bike_desig": props.get("bike_desig", 0),
        "prohib": props.get("prohib", []),
        "dest": props.get("dest", []),
        "conns": props["conns"],
    }
    coords = [[lon, lat] for lon, lat in (m_to_lonlat(dx, dy) for dx, dy in points)]
    return {
        "type": "Feature",
        "id": sid,
        "properties": p,
        "geometry": {"type": "LineString", "coordinates": coords},
    }


def road_features():
    """The synthetic Overture-style road extract (as GeoJSON features)."""
    return [
        _seg("s-main1", "Main Street", "residential", [(0, 0), (100, 0)], conns=[["conn-A", 0.0], ["conn-B", 1.0]]),
        _seg("s-main2", "Main Street", "residential", [(100, 0), (200, 0)], conns=[["conn-B", 0.0], ["conn-C", 1.0]]),
        _seg("s-side1", "Side Street", "residential", [(0, -100), (100, -100)], conns=[["conn-D", 0.0], ["conn-E", 1.0]]),
        _seg("s-side2", "Side Street", "residential", [(100, -100), (200, -100)], conns=[["conn-E", 0.0], ["conn-F", 1.0]]),
        _seg("s-vert1", "Cross Street", "residential", [(0, 0), (0, -100)], conns=[["conn-A", 0.0], ["conn-D", 1.0]]),
        _seg("s-vert2", "Cycle Path", "cycleway", [(200, 0), (200, -100)], conns=[["conn-C", 0.0], ["conn-F", 1.0]]),
        _seg("s-oneway", "One Way Alley", "service", [(0, -100), (0, -200)], ow=1, conns=[["conn-D", 0.0], ["conn-G", 1.0]]),
        # secondary at 40 mph: an arterial (costly, but routed)
        _seg("s-fast", "Fast Road", "secondary", [(-100, 0), (0, 0)], speed=40, conns=[["conn-H", 0.0], ["conn-A", 1.0]]),
        # secondary at 20 mph: quiet (effective limit <= 20 mph)
        _seg("s-slow", "Slow Boulevard", "secondary", [(-100, -100), (0, -100)], speed=20, conns=[["conn-H2", 0.0], ["conn-D", 1.0]]),
        # stairs: excluded for every tier, even with an explicit access rule
        _seg("s-steps", "Stair Hill", "steps", [(200, -100), (250, -100)], conns=[["conn-F", 0.0], ["conn-X", 1.0]]),
        # motorway (no posted limit -> 65 mph default): excluded for every tier
        _seg("s-hwy", "Fast Freeway", "motorway", [(-100, 0), (-100, 100)], conns=[["conn-H", 0.0], ["conn-I", 1.0]]),
        # sidewalk: a high-cost last-resort connector
        _seg("s-walk", "Walkway", "footway", [(200, -100), (300, -100)], conns=[["conn-F", 0.0], ["conn-Y", 1.0]]),
        # traffic-calmed street
        _seg("s-living", "Garden Street", "living_street", [(-100, -100), (-100, -200)], conns=[["conn-H2", 0.0], ["conn-J", 1.0]]),
    ]


def make_road_graph(features=None):
    """Build a RoadGraph (union, with per-vehicle masks) from a synthetic extract."""
    from pev_buddy.graph import RoadGraph

    feats = road_features() if features is None else features
    connectors = {f"conn-{n}": list(m_to_lonlat(*p)) for n, p in NODES.items()}
    roads = {"type": "FeatureCollection", "features": feats}
    g = RoadGraph()
    g.build(roads, connectors)
    return g


@pytest.fixture()
def graph():
    return make_road_graph()


@pytest.fixture()
def pt():
    return m_to_lonlat
