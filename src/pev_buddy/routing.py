"""A* routing over the directed road graph with PEV constraints."""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import numpy as np

from . import config, geo
from .graph import RoadGraph


class RouteError(Exception):
    """No usable route between the two points."""


class SnapError(RouteError):
    """A point could not be snapped to the network within the search radius."""

    def __init__(self, point: tuple[float, float], nearest_node: int, nearest_m: float, radius_m: float):
        super().__init__(
            f"point {point} is farther than {radius_m:.0f} m from the PEV network "
            f"(nearest node is {nearest_m:.0f} m away)"
        )
        self.point = point
        self.nearest_node = nearest_node
        self.nearest_m = nearest_m


@dataclass
class RouteResult:
    edge_idxs: list[int]
    path: list[list[float]]
    distance_m: float
    duration_s: float
    start_node: int
    end_node: int
    start_snap_m: float = 0.0
    end_snap_m: float = 0.0
    warnings: list[str] = field(default_factory=list)
    # category -> {"distance_m": float, "max_speed_mph": int | None}
    segment_stats: dict = field(default_factory=dict)


def snap_to_node(
    graph: RoadGraph,
    lon: float,
    lat: float,
    vehicle: str = config.DEFAULT_VEHICLE,
    radius_m: float | None = None,
    prefer: str = "start",
) -> tuple[int, float]:
    """Nearest node usable by `vehicle`; raise SnapError if farther than radius_m.

    Starts prefer nodes with at least one allowed outgoing edge and
    destinations prefer nodes with an allowed incoming edge, so a point on a
    dead-end stub (e.g. a sidewalk endpoint, or a street the vehicle type may
    not ride) snaps to the nearest usable junction instead.
    """
    radius = radius_m if radius_m is not None else config.SNAP_RADIUS_M
    dists = geo.fast_distances(lon, lat, np.asarray(graph.node_coords, dtype=float))
    i = int(np.argmin(dists))
    d = float(dists[i])
    if d > radius:
        raise SnapError((lon, lat), i, d, radius)
    usable = graph.out_ok[vehicle] if prefer == "start" else graph.in_ok[vehicle]
    if not usable[i]:
        cand = np.where(np.asarray(usable, dtype=bool) & (dists <= radius))[0]
        if cand.size:
            j = int(cand[np.argmin(dists[cand])])
            return j, float(dists[j])
    return i, d


def _scc_reachable(adj: list[set[int]], src: int, dst: int) -> bool:
    """Is dst reachable from src in the (acyclic) SCC condensation DAG?"""
    seen = {src}
    stack = [src]
    while stack:
        x = stack.pop()
        for m in adj[x]:
            if m == dst:
                return True
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return False


def _heuristics(graph: RoadGraph, end_lon: float, end_lat: float, profile: str) -> np.ndarray:
    """Admissible per-node heuristic: straight-line meters * minimum cost factor.

    Equirectangular projection with a single reference latitude (the endpoint's),
    so points at equal longitude project to equal x.
    """
    factor = min(config.COST_PROFILES[profile].values())
    ref = math.cos(math.radians(end_lat)) * geo.EARTH_RADIUS_M
    x_end = math.radians(end_lon) * ref
    y_end = math.radians(end_lat) * geo.EARTH_RADIUS_M
    pts = np.asarray(graph.node_coords, dtype=float)
    x = np.radians(pts[:, 0]) * ref
    y = np.radians(pts[:, 1]) * geo.EARTH_RADIUS_M
    return np.hypot(x - x_end, y - y_end) * factor


def find_route(
    graph: RoadGraph,
    start_lon: float,
    start_lat: float,
    end_lon: float,
    end_lat: float,
    vehicle: str = config.DEFAULT_VEHICLE,
    snap_radius_m: float | None = None,
) -> RouteResult:
    """A* over states (node, incoming_edge), for a given CA vehicle type.

    The incoming edge is part of the state so that U-turn blocking and
    1-step prohibited transitions are applied correctly. Edges the vehicle
    type may not ride (per its road-access rules and Overture access mode)
    are skipped via the graph's per-vehicle allow bitmap.
    """
    veh = config.VEHICLE_TYPES.get(vehicle)
    if veh is None:
        raise ValueError(
            f"unknown vehicle type {vehicle!r}; expected one of: {', '.join(config.VEHICLE_ORDER)}"
        )
    if not veh.routable:
        raise RouteError(
            f"{veh.label} is not street-legal in California — off-highway use only. "
            "Choose a street-legal vehicle type."
        )

    start, start_d = snap_to_node(graph, start_lon, start_lat, vehicle, snap_radius_m, prefer="start")
    end, end_d = snap_to_node(graph, end_lon, end_lat, vehicle, snap_radius_m, prefer="end")

    if graph.scc is not None:
        c0, c1 = graph.scc[start], graph.scc[end]
        if c0 != c1 and not _scc_reachable(graph.scc_reach, c0, c1):
            raise RouteError(
                f"no {veh.label.lower()} route: start and destination are not connected on "
                "roads this vehicle type may use"
            )

    if start == end:
        return RouteResult(
            edge_idxs=[],
            path=[list(graph.node(start))],
            distance_m=0.0,
            duration_s=0.0,
            start_node=start,
            end_node=end,
            start_snap_m=start_d,
            end_snap_m=end_d,
        )

    ok = graph.edge_ok[vehicle]
    mult = [config.COST_PROFILES[veh.profile][c] for c in config.CATEGORIES]
    _E_frm, E_to, E_seg, E_head, E_len, E_cat = graph.edge_columns()
    h = _heuristics(graph, end_lon, end_lat, veh.profile)
    START = -1
    open_heap: list[tuple] = []  # (f, g, tie, node, in_edge)
    best: dict[tuple[int, int], float] = {(start, START): 0.0}
    parent: dict[tuple[int, int], tuple[int, int]] = {}
    tie = 0
    heapq.heappush(open_heap, (h[start], 0.0, tie, start, START))

    while open_heap:
        f, g, _, node, in_edge = heapq.heappop(open_heap)
        state = (node, in_edge)
        if g > best[state] + 1e-9:
            continue  # stale entry
        if node == end:
            # with an admissible heuristic, the first popped goal state is optimal
            return _reconstruct(graph, parent, state, start, end, start_d, end_d, veh)

        for e in graph.adj[node]:
            if not ok[e]:
                continue  # this vehicle type may not ride this edge
            b = E_to[e]
            seg_idx = E_seg[e]
            heading = E_head[e]
            if in_edge != START:
                prev_seg = E_seg[in_edge]
                prev_heading = E_head[in_edge]
                # immediate U-turn on the same segment is not a legal maneuver
                if prev_seg == seg_idx and prev_heading != heading:
                    continue
                mask = 1 if prev_heading == 0 else 2
                forbidden = graph.prohib.get((prev_seg, mask, node))
                if forbidden is not None and seg_idx in forbidden:
                    continue
            nstate = (b, e)
            ng = g + E_len[e] * mult[E_cat[e]]
            if ng < best.get(nstate, float("inf")) - 1e-9:
                best[nstate] = ng
                parent[nstate] = state
                tie += 1
                heapq.heappush(open_heap, (ng + h[b], ng, tie, b, e))

    raise RouteError(
        f"no {veh.label.lower()} route found between the given points — the destination is "
        "not reachable within this vehicle type's road-access rules"
    )


def _reconstruct(
    graph: RoadGraph,
    parent: dict,
    goal: tuple[int, int],
    start: int,
    end: int,
    start_d: float,
    end_d: float,
    veh: config.VehicleType,
) -> RouteResult:
    edges: list[int] = []
    state = goal
    start_state = (start, -1)
    while state != start_state:
        e = state[1]
        edges.append(e)
        state = parent[state]
    edges.reverse()

    path: list[list[float]] = []
    total = 0.0
    duration = 0.0
    stats: dict[str, dict] = {}
    for i, e in enumerate(edges):
        geom = graph.edge_geometry(e)
        for c in geom if i == 0 else geom[1:]:
            if not path or path[-1] != c:
                path.append(c)
        _a, _b, _s, _h, length, category, eff, posted = graph.edges[e]
        total += length
        # Per-edge ETA from vehicle cap x road limit (was a flat 15 mph).
        duration += config.edge_duration_s(veh, length, category, eff)
        st = stats.setdefault(category, {"distance_m": 0.0, "max_speed_mph": None})
        st["distance_m"] += length
        if posted is not None:
            st["max_speed_mph"] = max(st["max_speed_mph"] or 0, posted)
    return RouteResult(
        edge_idxs=edges,
        path=path,
        distance_m=total,
        duration_s=duration,
        start_node=start,
        end_node=end,
        start_snap_m=start_d,
        end_snap_m=end_d,
        segment_stats=stats,
    )
