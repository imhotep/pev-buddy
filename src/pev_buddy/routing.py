"""A* routing over the directed road graph with PEV constraints."""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import numpy as np

from . import config, geo
from .graph import RoadGraph


class RouteError(Exception):
    """No usable route between the two points.

    `str(e)` is the technical account, for logs. `code` is a stable id a
    client can branch on, and `user_message` is what to show a rider: plain
    words, no coordinates or internals.
    """

    def __init__(self, message: str, code: str = "no_route", user_message: str | None = None):
        super().__init__(message)
        self.code = code
        self.user_message = user_message or "No route found between these points."


class SnapError(RouteError):
    """A point could not be snapped to the network within the search radius."""

    def __init__(
        self, point: tuple[float, float], nearest_node: int, nearest_m: float, radius_m: float, which: str = "point"
    ):
        place = {"start": "The start", "end": "The destination"}.get(which, "That spot")
        super().__init__(
            f"{which} {point} is farther than {radius_m:.0f} m from the PEV network "
            f"(nearest node is {nearest_m:.0f} m away)",
            code="off_network",
            user_message=f"{place} isn't near a street PEV Buddy can route on. Pick a point closer to a road.",
        )
        self.point = point
        self.nearest_node = nearest_node
        self.nearest_m = nearest_m
        self.which = which


# Off-street bicycle facilities (OSM-style classes in the Overture extract).
BIKE_PATH_CLASSES = frozenset({"cycleway", "path", "track"})

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
    climb_m: float = 0.0  # total ascent along the route (m)
    descent_m: float = 0.0
    # Off-street bike paths (cycleway / path / track) on the route, in order:
    # [{"name": str | None, "near": str | None, "distance_m": float}], `near`
    # naming the street an unnamed path leaves from. A street merely signed as
    # a bike route counts as "bike_lane" in segment_stats but not here.
    bike_paths: list[dict] = field(default_factory=list)
    # Per edge in edge_idxs, the (from, to) fraction of it ridden: (0, 1)
    # except where a trip starts or ends partway along a street.
    edge_parts: list[tuple[float, float]] = field(default_factory=list)
    # Stretches ridden in mixed traffic (config.MIXED_TRAFFIC: a street with
    # no bike lane or path), as [lon, lat] polylines, and their total length.
    traffic_paths: list[list[list[float]]] = field(default_factory=list)
    traffic_m: float = 0.0


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
        raise SnapError((lon, lat), i, d, radius, which="start" if prefer == "start" else "end")
    usable = graph.out_ok[vehicle] if prefer == "start" else graph.in_ok[vehicle]
    cand = np.where(np.asarray(usable, dtype=bool) & (dists <= radius))[0]
    if not cand.size:
        return i, d
    cand = cand[np.argsort(dists[cand], kind="stable")]
    # Skip junctions in a pocket this vehicle can't get in or out of: the
    # Sutter-Stockton garage's BikeLink locker snapped to a 64 m stub of
    # Stockton St whose neighbours are closed to bikes, so no route reached
    # it. The nearest junction joined to the wider network wins; when none
    # within reach is (a tiny network), the nearest usable one, as before.
    for j in cand[: config.SNAP_MAX_CANDIDATES]:
        if _joined(graph, int(j), vehicle, forward=prefer == "start"):
            return int(j), float(dists[j])
    j = int(cand[0])
    return j, float(dists[j])


def _joined(graph: RoadGraph, node: int, vehicle: str, forward: bool) -> bool:
    """Can `vehicle` ride from `node` (forward) or reach it (backward) to/from
    more than config.SNAP_POCKET_NODES other junctions? A bounded search: it
    stops as soon as the answer is yes."""
    ok = graph.edge_ok[vehicle]
    frm, to = graph.edge_columns()[:2]
    nbrs = graph.adj if forward else graph.incoming()
    seen = {node}
    stack = [node]
    while stack:
        x = stack.pop()
        for e in nbrs[x]:
            if not ok[e]:
                continue
            y = to[e] if forward else frm[e]
            if y not in seen:
                if len(seen) > config.SNAP_POCKET_NODES:
                    return True
                seen.add(y)
                stack.append(y)
    return False


# ---------------------------------------------------------------- mid-street ends


@dataclass
class _Anchor:
    """A trip end partway along a directed edge: `frac` of the way from its
    start junction to its end junction (by length)."""

    edge: int
    frac: float
    dist_m: float


def _project(geom: list[list[float]], lon: float, lat: float) -> tuple[float, float]:
    """(meters from the point to the polyline, fraction of the polyline's
    length where it is closest). Local flat projection."""
    kx = math.cos(math.radians(lat)) * 111_320.0
    ky = 110_574.0
    best_d2, best_along, total = float("inf"), 0.0, 0.0
    for (ax, ay), (bx, by) in zip(geom, geom[1:]):
        dx, dy = (bx - ax) * kx, (by - ay) * ky
        L = math.hypot(dx, dy)
        px, py = (lon - ax) * kx, (lat - ay) * ky
        t = 0.0 if L == 0 else max(0.0, min(1.0, (px * dx + py * dy) / (L * L)))
        d2 = (px - t * dx) ** 2 + (py - t * dy) ** 2
        if d2 < best_d2:
            best_d2, best_along = d2, total + t * L
        total += L
    return math.sqrt(best_d2), (best_along / total if total > 0 else 0.0)


def sub_geometry(geom: list[list[float]], f0: float, f1: float) -> list[list[float]]:
    """The part of a polyline between length fractions f0 <= f1."""
    if f0 <= 0.0 and f1 >= 1.0:
        return [list(c) for c in geom]
    kx = math.cos(math.radians(geom[0][1])) * 111_320.0
    ky = 110_574.0
    seg = [math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * ky) for a, b in zip(geom, geom[1:])]
    total = sum(seg)
    if total == 0:
        return [list(geom[0])]

    def at(f: float) -> tuple[int, list[float]]:
        want = f * total
        run = 0.0
        for i, L in enumerate(seg):
            if run + L >= want or i == len(seg) - 1:
                t = 0.0 if L == 0 else min(1.0, max(0.0, (want - run) / L))
                a, b = geom[i], geom[i + 1]
                return i, [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
            run += L
        return len(seg) - 1, list(geom[-1])

    i0, p0 = at(f0)
    i1, p1 = at(f1)
    out = [p0, *[list(c) for c in geom[i0 + 1 : i1 + 1]], p1]
    dedup = [out[0]]
    for c in out[1:]:
        if c != dedup[-1]:
            dedup.append(c)
    return dedup


def _edge_anchors(
    graph: RoadGraph, lon: float, lat: float, vehicle: str, radius: float, which: str
) -> list[_Anchor]:
    """Where a trip end sits on the street network: the nearest point on a
    street this vehicle may ride, as one anchor per direction it may be ridden
    there. [] means "use the nearest junction" (the point is at one, or no
    street is near enough; snap_to_node then decides or raises)."""
    coords = np.asarray(graph.node_coords, dtype=float)
    dists = geo.fast_distances(lon, lat, coords)
    if not len(dists) or float(dists.min()) <= config.SNAP_NODE_M:
        return []
    k = min(config.SNAP_EDGE_NODES, len(dists))
    near = np.argpartition(dists, k - 1)[:k]
    ok = graph.edge_ok[vehicle]
    frm, to, seg_col, _head, _len, cat = graph.edge_columns()
    incoming = graph.incoming()
    sidewalk = config.CATEGORIES.index("sidewalk")
    candidates: dict[int, _Anchor] = {}
    for n in near.tolist():
        for e in list(graph.adj[n]) + list(incoming[n]):
            if e in candidates or not ok[e]:
                continue
            d, f = _project(graph.edge_geometry(e), lon, lat)
            if d <= radius:
                candidates[e] = _Anchor(e, f, d)
    if not candidates:
        return []
    start = which == "start"

    def rank(a: _Anchor) -> float:
        return a.dist_m + (config.SNAP_SIDEWALK_SLACK_M if cat[a.edge] == sidewalk else 0.0)

    ordered = sorted(candidates.values(), key=rank)

    def joined(a: _Anchor) -> bool:
        return _joined(graph, to[a.edge] if start else frm[a.edge], vehicle, forward=start)

    # The nearest street joined to the wider network (see snap_to_node's
    # pockets): judged at the junction a start rides to, or an end comes
    # from. When none within reach is (a tiny network), the nearest street.
    best = next((a for a in ordered[: config.SNAP_MAX_CANDIDATES] if joined(a)), None)
    need_joined = best is not None
    best = best or ordered[0]
    # At a street's end (a corner) the trip starts or ends at that junction,
    # free to turn any way, as before.
    length = _len[best.edge]
    if min(best.frac, 1.0 - best.frac) * length <= config.SNAP_NODE_M:
        return []
    # Every direction of that street at that spot (a two-way street is two
    # edges over one geometry): ride off, or arrive, either way.
    same = [
        a for a in candidates.values()
        if seg_col[a.edge] == seg_col[best.edge] and abs(a.dist_m - best.dist_m) < 0.5
        and (not need_joined or joined(a))
    ]
    return same or [best]


@dataclass
class WalkResult:
    path: list[list[float]]
    distance_m: float
    duration_s: float


def find_walk(
    graph: RoadGraph,
    start_lon: float,
    start_lat: float,
    end_lon: float,
    end_lat: float,
    snap_radius_m: float | None = None,
) -> WalkResult:
    """Walking route, e.g. from a parking spot to the real destination.

    A walker uses every street, path, sidewalk and stair in the graph, in
    either direction (one-way rules and turn restrictions are for vehicles),
    so the search runs over edges forwards and backwards with length as the
    cost. Freeways and other > 45 mph roads are not in the graph at all.
    """
    radius = snap_radius_m if snap_radius_m is not None else config.SNAP_RADIUS_M
    coords = np.asarray(graph.node_coords, dtype=float)
    ends = []
    for which, lon, lat in (("start", start_lon, start_lat), ("end", end_lon, end_lat)):
        d = geo.fast_distances(lon, lat, coords)
        i = int(np.argmin(d))
        if float(d[i]) > radius:
            raise SnapError((lon, lat), i, float(d[i]), radius, which=which)
        ends.append(i)
    start, end = ends
    if start == end:
        return WalkResult(path=[list(graph.node(start))], distance_m=0.0, duration_s=0.0)

    frm, to, _seg, _head, length, _cat = graph.edge_columns()
    incoming = graph.incoming()
    ref = math.cos(math.radians(end_lat)) * geo.EARTH_RADIUS_M
    xe, ye = math.radians(end_lon) * ref, math.radians(end_lat) * geo.EARTH_RADIUS_M

    def h(n: int) -> float:
        lon, lat = coords[n]
        return math.hypot(math.radians(lon) * ref - xe, math.radians(lat) * geo.EARTH_RADIUS_M - ye)

    best = {start: 0.0}
    parent: dict[int, tuple[int, int, bool]] = {}  # node -> (prev node, edge, edge walked forwards)
    heap = [(h(start), 0.0, start)]
    while heap:
        _f, g, n = heapq.heappop(heap)
        if g > best[n] + 1e-9:
            continue
        if n == end:
            break
        for e, nxt, forward in [(e, to[e], True) for e in graph.adj[n]] + [(e, frm[e], False) for e in incoming[n]]:
            ng = g + length[e]
            if ng < best.get(nxt, float("inf")) - 1e-9:
                best[nxt] = ng
                parent[nxt] = (n, e, forward)
                heapq.heappush(heap, (ng + h(nxt), ng, nxt))
    if end not in best:
        raise RouteError(
            "no walking route found between the given points",
            code="no_walk",
            user_message="No walking route found between these points.",
        )

    legs = []
    n = end
    while n != start:
        prev, e, forward = parent[n]
        legs.append((e, forward))
        n = prev
    legs.reverse()
    path: list[list[float]] = []
    for e, forward in legs:
        geom = graph.edge_geometry(e)
        if not forward:
            geom = list(reversed(geom))
        for c in geom:
            if not path or path[-1] != c:
                path.append(c)
    # The search runs junction to junction, but the parking spot and the
    # destination are usually mid-block: the walk would overshoot to the
    # corner and come back. Cut it where it passes closest to each end.
    path = _trim_to_ends(path, (start_lon, start_lat), (end_lon, end_lat))
    distance = geo.linestring_length_m([tuple(c) for c in path]) if len(path) > 1 else 0.0
    return WalkResult(path=path, distance_m=distance, duration_s=distance / (config.WALK_MPH * config.MPH_TO_MPS))


def _closest_on_path(path: list[list[float]], pt: tuple[float, float], lo: int = 0, hi: int | None = None) -> tuple[int, list[float]]:
    """(segment index i, point) of the point on path[lo..hi] nearest pt,
    lying on segment path[i] -> path[i + 1]. Local flat projection: the
    segments are a few metres to a few hundred long."""
    hi = len(path) - 1 if hi is None else hi
    kx = math.cos(math.radians(pt[1]))
    best = (float("inf"), lo, list(path[lo]))
    for i in range(lo, max(lo, hi)):
        (ax, ay), (bx, by) = path[i], path[i + 1]
        dx, dy = (bx - ax) * kx, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((pt[0] - ax) * kx * dx + (pt[1] - ay) * dy) / L2))
        px, py = ax + (bx - ax) * t, ay + (by - ay) * t
        d = ((px - pt[0]) * kx) ** 2 + (py - pt[1]) ** 2
        if d < best[0]:
            best = (d, i, [px, py])
    return best[1], best[2]


def _trim_to_ends(path: list[list[float]], start: tuple[float, float], end: tuple[float, float]) -> list[list[float]]:
    if len(path) < 2:
        return path
    ei, ept = _closest_on_path(path, end)
    si, spt = _closest_on_path(path, start, 0, ei + 1)
    if si == ei:
        # Both cuts on one segment: keep the piece between them.
        return [spt, ept] if spt != ept else [spt]
    return [spt, *path[si + 1 : ei + 1], ept]


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
        msg = f"{veh.label} is not street-legal in California — off-highway use only. Choose a street-legal vehicle type."
        raise RouteError(msg, code="vehicle_not_street_legal", user_message=msg)

    radius = snap_radius_m if snap_radius_m is not None else config.SNAP_RADIUS_M
    frm_col, to_col = graph.edge_columns()[:2]
    # Each end is partway along a street (anchors), or at a junction.
    s_anchors = _edge_anchors(graph, start_lon, start_lat, vehicle, radius, "start")
    e_anchors = _edge_anchors(graph, end_lon, end_lat, vehicle, radius, "end")
    if s_anchors:
        start, start_d = to_col[s_anchors[0].edge], s_anchors[0].dist_m
    else:
        start, start_d = snap_to_node(graph, start_lon, start_lat, vehicle, snap_radius_m, prefer="start")
    if e_anchors:
        end, end_d = frm_col[e_anchors[0].edge], e_anchors[0].dist_m
    else:
        end, end_d = snap_to_node(graph, end_lon, end_lat, vehicle, snap_radius_m, prefer="end")

    if graph.scc is not None:
        starts = [to_col[a.edge] for a in s_anchors] or [start]
        ends = [frm_col[a.edge] for a in e_anchors] or [end]
        if not any(
            graph.scc[s0] == graph.scc[e0] or _scc_reachable(graph.scc_reach, graph.scc[s0], graph.scc[e0])
            for s0 in starts
            for e0 in ends
        ):
            raise RouteError(
                f"no {veh.label.lower()} route: start and destination are not connected on "
                "roads this vehicle type may use",
                code="not_connected",
                user_message=f"No {veh.label.lower()} route: these points aren't connected by streets "
                f"a {veh.label.lower()} may use.",
            )

    if start == end and not s_anchors and not e_anchors:
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
    is_moto = veh.access_mode == "motorcycle"
    E_hill = graph.hill_costs(veh.profile)  # >= 0, so the heuristic stays admissible
    h = _heuristics(graph, end_lon, end_lat, veh.profile)
    START = -1
    GOAL = -2  # the end point partway along an edge, as a search state (GOAL, edge)

    def part_cost(e: int, f0: float, f1: float) -> float:
        return (f1 - f0) * (E_len[e] * mult[E_cat[e]] + E_hill[e])

    targets = {a.edge: a.frac for a in e_anchors}
    open_heap: list[tuple] = []  # (f, g, tie, node, in_edge)
    best: dict[tuple[int, int], float] = {}
    parent: dict[tuple[int, int], tuple[int, int] | None] = {}
    tie = 0

    def push(state: tuple[int, int], g: float, hval: float, from_state) -> None:
        nonlocal tie
        if g < best.get(state, float("inf")) - 1e-9:
            best[state] = g
            parent[state] = from_state
            tie += 1
            heapq.heappush(open_heap, (g + hval, g, tie, state[0], state[1]))

    if s_anchors:
        # Ride off from the start point along each direction of its street;
        # the in-edge is that street, so turn rules apply at its end.
        for a in s_anchors:
            push((to_col[a.edge], a.edge), part_cost(a.edge, a.frac, 1.0), h[to_col[a.edge]], None)
            if a.edge in targets and targets[a.edge] >= a.frac:
                # Start and end on one stretch of street, in riding order.
                push((GOAL, a.edge), part_cost(a.edge, a.frac, targets[a.edge]), 0.0, None)
    else:
        push((start, START), 0.0, h[start], None)

    while open_heap:
        f, g, _, node, in_edge = heapq.heappop(open_heap)
        state = (node, in_edge)
        if g > best[state] + 1e-9:
            continue  # stale entry
        if node == GOAL or (not e_anchors and node == end):
            # with an admissible heuristic, the first popped goal state is optimal
            return _reconstruct(
                graph, parent, state, start, end, start_d, end_d, veh,
                start_frac={a.edge: a.frac for a in s_anchors}, end_frac=targets,
            )

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
                if is_moto:
                    forbidden = graph.prohib_moto.get((prev_seg, mask, node))
                    if forbidden is not None and seg_idx in forbidden:
                        continue  # a car rule bicycles are exempt from
            if e in targets:
                # The end point is partway along this edge.
                push((GOAL, e), g + part_cost(e, 0.0, targets[e]), 0.0, state)
            push((b, e), g + E_len[e] * mult[E_cat[e]] + E_hill[e], h[b], state)

    raise RouteError(
        f"no {veh.label.lower()} route found between the given points — the destination is "
        "not reachable within this vehicle type's road-access rules",
        code="no_route",
        user_message=f"No {veh.label.lower()} route found: the destination can't be reached on "
        f"streets a {veh.label.lower()} may use.",
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
    start_frac: dict[int, float] | None = None,
    end_frac: dict[int, float] | None = None,
) -> RouteResult:
    edges: list[int] = []
    state = goal
    while state is not None and state[1] != -1:
        edges.append(state[1])
        state = parent[state]
    edges.reverse()
    parts = [(0.0, 1.0) for _ in edges]
    if edges and start_frac and edges[0] in start_frac:
        parts[0] = (start_frac[edges[0]], parts[0][1])
    if edges and goal[0] == -2:
        parts[-1] = (parts[-1][0], end_frac[edges[-1]])

    path: list[list[float]] = []
    total = 0.0
    duration = 0.0
    climbs = graph.edge_climbs()
    climb_total = 0.0
    descent_total = 0.0
    stats: dict[str, dict] = {}
    bike_paths: list[dict] = []
    traffic_paths: list[list[list[float]]] = []
    traffic_m = 0.0
    in_traffic = False
    for i, e in enumerate(edges):
        f0, f1 = parts[i]
        share = max(f1 - f0, 0.0)
        geom = sub_geometry(graph.edge_geometry(e), f0, f1)
        for c in geom if i == 0 else geom[1:]:
            if not path or path[-1] != c:
                path.append(c)
        _a, _b, _s, _h, length, category, eff, posted = graph.edges[e]
        length *= share
        total += length
        if category in config.MIXED_TRAFFIC and len(geom) > 1:
            traffic_m += length
            if in_traffic:
                traffic_paths[-1].extend(geom[1:])
            else:
                traffic_paths.append([list(c) for c in geom])
        in_traffic = category in config.MIXED_TRAFFIC
        # Per-edge ETA from vehicle cap x road limit, slowed uphill.
        duration += graph.edge_duration_s(e, veh, climbs) * share
        climb = float(climbs[e]) * share if len(climbs) else 0.0
        climb_total += max(climb, 0.0)
        descent_total += max(-climb, 0.0)
        st = stats.setdefault(category, {"distance_m": 0.0, "max_speed_mph": None})
        st["distance_m"] += length
        if posted is not None:
            st["max_speed_mph"] = max(st["max_speed_mph"] or 0, posted)
        seg = graph.segments[_s]
        if seg["class"] in BIKE_PATH_CLASSES:
            if bike_paths and bike_paths[-1]["name"] == seg["name"] and bike_paths[-1]["last"] == i - 1:
                bike_paths[-1]["distance_m"] += length
                bike_paths[-1]["last"] = i
            else:
                # An unnamed path is placed by the street it leaves from.
                near = None
                for prev in reversed(edges[:i]):
                    prev_seg = graph.segments[graph.edges[prev][2]]
                    if prev_seg["name"] and prev_seg["class"] not in BIKE_PATH_CLASSES:
                        near = prev_seg["name"]
                        break
                bike_paths.append({"name": seg["name"], "near": near, "distance_m": length, "last": i})
    for p in bike_paths:
        del p["last"]
    if not path:
        path = [list(graph.node(start))]
    if edges:
        start = graph.edges[edges[0]][0] if parts[0][0] < 0.5 else graph.edges[edges[0]][1]
        end = graph.edges[edges[-1]][1] if parts[-1][1] > 0.5 else graph.edges[edges[-1]][0]
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
        climb_m=climb_total,
        descent_m=descent_total,
        bike_paths=bike_paths,
        edge_parts=parts,
        traffic_paths=traffic_paths,
        traffic_m=traffic_m,
    )
