"""Build a directed road graph from trimmed Overture segments + connectors."""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import orjson

from . import config, geo


class RoadGraph:
    """Directed graph of the road network, with per-vehicle-type access.

    Nodes are Overture connectors (integer indices); edges are directed
    half-edges along road segments. The graph is the *union* of everything
    any vehicle type may ride; which edges a given vehicle type may actually
    use is captured by the per-vehicle bitmaps built at the end of build().

    Instances are created either by build() (from a parsed Overture extract;
    used by sync and tests) or load_bundle() (from the compact graph.npz +
    graph_meta.json artifacts written by save_bundle(); used by the server).
    """

    def __init__(self):
        self.node_coords: np.ndarray = np.empty((0, 2))  # [lon, lat] per node index
        self.node_index: dict[str, int] = {}
        self.adj: list[list[int]] = []  # node -> outgoing edge indices
        self._edge_cols: tuple | None = None  # cached memoryviews, see edge_columns()
        # segments[seg_idx] = dict(id, name, class, coords, cums, length, conns)
        #   coords: (P, 2) float64 array of [lon, lat]; cums: (P,) cumulative
        #   meters; conns: list of (node_idx, at_fraction) sorted by at
        self.segments: list[dict] = []
        # edges[edge_idx] = (from_node, to_node, seg_idx, heading, length_m,
        #                    category, effective_speed_mph, posted_speed_mph_or_None)
        # build() stores a list of tuples; load_bundle() stores an EdgeTable
        # (columnar) with the same indexing/unpacking contract.
        self.edges = []
        # prohib: (seg_idx, heading_mask, from_node) -> set of forbidden to_seg_idx
        self.prohib: dict[tuple, set] = {}
        # dest: seg_idx -> [(to_seg_idx, to_node, label, heading_mask)]
        self.dest: dict[int, list] = {}
        # Per vehicle type (routable ids only): 1-byte allow flags.
        self.edge_ok: dict[str, bytearray] = {}  # edge_idx -> 1 when the type may ride it
        self.out_ok: dict[str, bytearray] = {}  # node -> 1 when it has an allowed outgoing edge
        self.in_ok: dict[str, bytearray] = {}  # node -> 1 when it has an allowed incoming edge
        self.edge_counts: dict[str, int] = {}
        # strongly connected component id per node (built by build()); None until
        # built, plus the condensation DAG used to fail fast on unreachable pairs.
        self.scc = None
        self.scc_reach: list[set[int]] = []

    # -- construction -------------------------------------------------------

    def build(self, roads_geojson: dict, connectors: dict) -> None:
        cids = list(connectors)
        self.node_index = {cid: i for i, cid in enumerate(cids)}
        self.node_coords = np.array([connectors[c] for c in cids], dtype=np.float64)
        self.adj = [[] for _ in cids]

        # Build-time-only per-segment fields (class/speed/access data). Kept in
        # a local parallel list and discarded at the end of build() so the
        # parsed Overture property dicts are never retained on the graph.
        meta: list[tuple] = []  # (cls, speed, ow, ow_moto, bike_desig) per segment
        seg_id_to_idx: dict[str, int] = {}
        # prohib/dest reference segment ids that may be registered later, so
        # collect the raw rows in pass 1 and resolve them after registration.
        raw_prohib: list[tuple] = []  # (seg_idx, connector_id, to_seg_id, heading_mask)
        raw_dest: list[tuple] = []  # (seg_idx, to_seg_id, connector_id, label, heading_mask)

        # Pass 1: register all segments so cross-references resolve.
        for feat in roads_geojson.get("features", []):
            p = feat["properties"]
            conns = sorted(p.get("conns") or [], key=lambda c: c[1])
            if len(conns) < 2:
                continue
            node_idxs = [self.node_index.get(cid) for cid, _ in conns]
            if any(n is None for n in node_idxs):
                continue  # connector outside our extract — skip segment

            cls = p.get("class") or "unknown"
            coords = np.asarray(feat["geometry"]["coordinates"], dtype=np.float64)
            length = geo.linestring_length_m([tuple(c) for c in coords])
            if length <= 0:
                continue
            cums = np.zeros(len(coords))
            for i in range(len(coords) - 1):
                cums[i + 1] = cums[i] + geo.haversine(*coords[i], *coords[i + 1])

            seg_idx = len(self.segments)
            cls = sys.intern(cls)
            self.segments.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "class": cls,
                    "coords": coords,
                    "cums": cums,
                    "length": length,
                    "conns": list(zip(node_idxs, (c[1] for c in conns))),
                }
            )
            meta.append(
                (
                    cls,
                    p.get("speed"),
                    p.get("ow", 3),
                    p.get("ow_moto", 3),
                    p.get("bike_desig", 0),
                )
            )
            for proh in p.get("prohib") or []:
                raw_prohib.append((seg_idx, proh[0], proh[1], proh[2]))
            for d in p.get("dest") or []:
                raw_dest.append((seg_idx, d[0], d[1], d[2], d[3]))
            seg_id_to_idx[p.get("id")] = seg_idx

        # Resolve cross-references now that every segment is registered.
        for seg_idx, cid, to_sid, hm in raw_prohib:
            to_seg = seg_id_to_idx.get(to_sid)
            at_node = self.node_index.get(cid)
            if to_seg is None or at_node is None:
                continue
            for hm_bit in (1, 2):
                if hm & hm_bit:
                    self.prohib.setdefault((seg_idx, hm_bit, at_node), set()).add(to_seg)
        for seg_idx, to_sid, cid, label, hm in raw_dest:
            to_seg = seg_id_to_idx.get(to_sid)
            to_node = self.node_index.get(cid)
            if to_seg is not None and to_node is not None:
                self.dest.setdefault(seg_idx, []).append((to_seg, to_node, label, hm))

        # Pass 2: edges.
        for seg_idx, seg in enumerate(self.segments):
            cls, speed, ow, ow_moto, bike_desig = meta[seg_idx]
            node_idxs = [n for n, _ in seg["conns"]]
            ats = [at for _, at in seg["conns"]]
            # Union access mask: a heading exists in the graph when ANY vehicle
            # type may ride it (bicycle denials or motorcycle denials, or both).
            ow_union = ow | ow_moto
            for heading in (0, 1):  # 0 = forward, 1 = backward
                bit = 1 if heading == 0 else 2
                if not (ow_union & bit):
                    continue
                category = self._edge_category(cls, speed, bool(bike_desig & bit))
                if category is None:
                    continue
                eff_speed = (
                    speed
                    if speed is not None
                    else config.SPEED_BY_CLASS_DEFAULT_MPH.get(cls, config.DEFAULT_SPEED_MPH)
                )
                seq = node_idxs if heading == 0 else list(reversed(node_idxs))
                use_ats = ats if heading == 0 else list(reversed(ats))
                for i in range(len(seq) - 1):
                    a, b = seq[i], seq[i + 1]
                    frac = abs(use_ats[i + 1] - use_ats[i])
                    if frac <= 1e-9:
                        continue
                    edge_len = seg["length"] * frac
                    edge_idx = len(self.edges)
                    self.edges.append((a, b, seg_idx, heading, edge_len, category, eff_speed, speed))
                    self.adj[a].append(edge_idx)

        self._build_vehicle_masks(meta)

        self.scc = self._build_scc()
        self.scc_reach = self._build_scc_reachability()

    def _build_vehicle_masks(self, meta: list[tuple]) -> None:
        """Per-vehicle-type allow bitmaps: which edges/nodes each type may use.

        An edge is usable by a type when (a) Overture access for that type's
        own mode allows the heading, and (b) the type's road-access rules
        allow the segment's category/speed.
        """
        n_edges = len(self.edges)
        n_nodes = len(self.node_coords)
        for vid in config.VEHICLE_ORDER:
            veh = config.VEHICLE_TYPES[vid]
            if not veh.routable:
                continue
            mask_idx = 3 if veh.access_mode == "motorcycle" else 2  # ow_moto / ow
            ok = bytearray(n_edges)
            out = bytearray(n_nodes)
            inn = bytearray(n_nodes)
            count = 0
            for i, edge in enumerate(self.edges):
                ow_flags = meta[edge[2]][mask_idx]
                heading_bit = 1 if edge[3] == 0 else 2
                if not (ow_flags & heading_bit):
                    continue
                if config.vehicle_allows(veh, edge[5], edge[6]):
                    ok[i] = 1
                    out[edge[0]] = 1
                    inn[edge[1]] = 1
                    count += 1
            self.edge_ok[vid] = ok
            self.out_ok[vid] = out
            self.in_ok[vid] = inn
            self.edge_counts[vid] = count

    def edge_cost(self, edge_idx: int, vehicle: str) -> float:
        """Cost of riding `edge_idx` on `vehicle`: length x profile multiplier."""
        veh = config.VEHICLE_TYPES[vehicle]
        _a, _b, _s, _h, length, category, _eff, _sp = self.edges[edge_idx]
        return length * config.COST_PROFILES[veh.profile][category]

    @staticmethod
    def _edge_category(cls: str, speed: float | None, desig: bool) -> str | None:
        """Cost category for a segment heading, or None when no vehicle type routes it.

        A posted limit wins over the class default; above MAX_SPEED_MPH the
        segment is a highway and is excluded for every vehicle type.
        """
        if cls in config.EXCLUDED_CLASSES:
            return None
        if desig or cls in ("cycleway", "path", "track"):
            return "bike_lane"
        if cls == "footway":
            return "sidewalk"
        if cls == "living_street":
            return "living_street"
        s = speed if speed is not None else config.SPEED_BY_CLASS_DEFAULT_MPH.get(cls, config.DEFAULT_SPEED_MPH)
        if s > config.MAX_SPEED_MPH:
            return None
        if s <= config.QUIET_MAX_MPH:
            return "quiet"
        if s <= config.SHARED_MAX_MPH:
            return "shared"
        return "arterial"

    def _build_scc_reachability(self) -> list[set[int]]:
        """Adjacency (deduped) of the SCC condensation DAG.

        c0 can route to c1 iff c1 is reachable from c0 in this DAG; the DAG is
        acyclic, so a reachability test is cheap and exact.
        """
        k = (max(self.scc) + 1) if len(self.scc) else 0
        adj: list[set[int]] = [set() for _ in range(k)]
        for a, b, *_ in self.edges:
            ca, cb = self.scc[a], self.scc[b]
            if ca != cb:
                adj[ca].add(cb)
        return adj

    def _build_scc(self) -> list[int]:
        """Strongly connected component id per node (iterative Kosaraju).

        A directed route from A to B may exist even when A and B are in
        different SCCs (e.g. a one-way alley into a dead end); use
        _build_scc_reachability + the condensation DAG for the reachability
        check.
        """
        n = len(self.node_coords)
        visited = [False] * n
        order: list[int] = []
        for start in range(n):
            if visited[start]:
                continue
            visited[start] = True
            stack = [(start, iter(self.adj[start]))]
            while stack:
                node, it = stack[-1]
                pushed = False
                for e in it:  # adj stores edge indices; follow to the target node
                    m = self.edges[e][1]
                    if not visited[m]:
                        visited[m] = True
                        stack.append((m, iter(self.adj[m])))
                        pushed = True
                        break
                if not pushed:
                    order.append(node)
                    stack.pop()

        radj: list[list[int]] = [[] for _ in range(n)]
        for a, b, *_ in self.edges:
            radj[b].append(a)

        scc = [-1] * n
        cid = 0
        for start in reversed(order):
            if scc[start] != -1:
                continue
            scc[start] = cid
            stack = [start]
            while stack:
                x = stack.pop()
                for m in radj[x]:
                    if scc[m] == -1:
                        scc[m] = cid
                        stack.append(m)
            cid += 1
        return scc

    # -- bundle (compact runtime artifacts) ----------------------------------

    def save_bundle(self, data_dir: str | Path) -> None:
        """Write graph.npz (numeric arrays) + graph_meta.json (ids/strings/maps)."""
        data_dir = Path(data_dir)
        cat_idx = {c: i for i, c in enumerate(config.CATEGORIES)}
        e = self.edges
        segs = self.segments
        n_seg = len(segs)

        coord_offsets = np.zeros(n_seg + 1, dtype=np.int64)
        conn_offsets = np.zeros(n_seg + 1, dtype=np.int64)
        for i, s in enumerate(segs):
            coord_offsets[i + 1] = coord_offsets[i] + len(s["coords"])
            conn_offsets[i + 1] = conn_offsets[i] + len(s["conns"])
        coords_flat = np.concatenate([s["coords"] for s in segs]) if n_seg else np.empty((0, 2))
        cums_flat = np.concatenate([s["cums"] for s in segs]) if n_seg else np.empty((0,))
        conn_node = np.array([n for s in segs for n, _ in s["conns"]], dtype=np.int32)
        conn_at = np.array([at for s in segs for _, at in s["conns"]], dtype=np.float64)

        adj_offsets = np.zeros(len(self.adj) + 1, dtype=np.int64)
        for i, lst in enumerate(self.adj):
            adj_offsets[i + 1] = adj_offsets[i] + len(lst)
        adj_flat = np.array([ei for lst in self.adj for ei in lst], dtype=np.int32)

        reach_offsets = np.zeros(len(self.scc_reach) + 1, dtype=np.int64)
        for i, st in enumerate(self.scc_reach):
            reach_offsets[i + 1] = reach_offsets[i] + len(st)
        reach_flat = np.array([m for st in self.scc_reach for m in sorted(st)], dtype=np.int32)

        vehicles = [v for v in config.VEHICLE_ORDER if v in self.edge_ok]

        def mask_rows(d: dict[str, bytearray]) -> np.ndarray:
            return np.stack([np.frombuffer(bytes(d[v]), dtype=np.uint8) for v in vehicles])

        np.savez(
            data_dir / "graph.npz",
            node_coords=np.asarray(self.node_coords, dtype=np.float64),
            edge_from=np.array([x[0] for x in e], dtype=np.int32),
            edge_to=np.array([x[1] for x in e], dtype=np.int32),
            edge_seg=np.array([x[2] for x in e], dtype=np.int32),
            edge_heading=np.array([x[3] for x in e], dtype=np.uint8),
            edge_len=np.array([x[4] for x in e], dtype=np.float64),
            edge_cat=np.array([cat_idx[x[5]] for x in e], dtype=np.uint8),
            edge_eff=np.array([x[6] for x in e], dtype=np.float64),
            edge_posted=np.array([x[7] if x[7] is not None else math.nan for x in e], dtype=np.float64),
            adj_offsets=adj_offsets,
            adj_flat=adj_flat,
            seg_coord_offsets=coord_offsets,
            coords_flat=coords_flat,
            cums_flat=cums_flat,
            conn_offsets=conn_offsets,
            conn_node=conn_node,
            conn_at=conn_at,
            seg_length=np.array([s["length"] for s in segs], dtype=np.float64),
            scc=np.asarray(self.scc, dtype=np.int32),
            reach_offsets=reach_offsets,
            reach_flat=reach_flat,
            mask_edge_ok=mask_rows(self.edge_ok),
            mask_out_ok=mask_rows(self.out_ok),
            mask_in_ok=mask_rows(self.in_ok),
        )

        cids: list[str | None] = [None] * len(self.node_index)
        for cid, i in self.node_index.items():
            cids[i] = cid
        meta = {
            "seg_ids": [s["id"] for s in segs],
            "seg_names": [s["name"] for s in segs],
            "seg_classes": [s["class"] for s in segs],
            "connector_ids": cids,
            "vehicles": vehicles,
            "prohib": [[k[0], k[1], k[2], sorted(v)] for k, v in self.prohib.items()],
            "dest": [[s, *row] for s, rows in self.dest.items() for row in rows],
        }
        (data_dir / "graph_meta.json").write_bytes(orjson.dumps(meta))

    @classmethod
    def load_bundle(cls, data_dir: str | Path) -> "RoadGraph":
        """Rebuild a graph from the artifacts written by save_bundle()."""
        data_dir = Path(data_dir)
        z = np.load(data_dir / "graph.npz")
        meta = orjson.loads((data_dir / "graph_meta.json").read_bytes())

        g = cls()
        g.node_coords = z["node_coords"]
        cids: list[str] = meta["connector_ids"]
        g.node_index = {cid: i for i, cid in enumerate(cids)}

        adj_offsets, adj_flat = z["adj_offsets"], z["adj_flat"]
        g.adj = [adj_flat[adj_offsets[i] : adj_offsets[i + 1]].tolist() for i in range(len(cids))]

        ef, et, es = z["edge_from"], z["edge_to"], z["edge_seg"]
        eh, el, ec = z["edge_heading"], z["edge_len"], z["edge_cat"]
        ee, ep = z["edge_eff"], z["edge_posted"]
        g.edges = EdgeTable(ef, et, es, eh, el, ec, ee, ep)

        co, cf, cu = z["seg_coord_offsets"], z["coords_flat"], z["cums_flat"]
        ko, kn, ka = z["conn_offsets"], z["conn_node"], z["conn_at"]
        seg_ids, seg_names, seg_length = meta["seg_ids"], meta["seg_names"], z["seg_length"]
        seg_classes = [sys.intern(c) for c in meta["seg_classes"]]
        g.segments = [
            {
                "id": seg_ids[i],
                "name": seg_names[i],
                "class": seg_classes[i],
                "length": float(seg_length[i]),
                "coords": cf[co[i] : co[i + 1]],
                "cums": cu[co[i] : co[i + 1]],
                "conns": [(int(kn[j]), float(ka[j])) for j in range(ko[i], ko[i + 1])],
            }
            for i in range(len(seg_ids))
        ]

        g.scc = z["scc"]
        ro, rf = z["reach_offsets"], z["reach_flat"]
        g.scc_reach = [set(rf[ro[i] : ro[i + 1]].tolist()) for i in range(len(ro) - 1)]

        g.prohib = {(r[0], r[1], r[2]): set(r[3]) for r in meta["prohib"]}
        for seg_idx, to_seg, to_node, label, hm in meta["dest"]:
            g.dest.setdefault(seg_idx, []).append((to_seg, to_node, label, hm))

        mok, mout, min_ = z["mask_edge_ok"], z["mask_out_ok"], z["mask_in_ok"]
        for row, vid in enumerate(meta["vehicles"]):
            ok = bytearray(mok[row])
            g.edge_ok[vid] = ok
            g.out_ok[vid] = bytearray(mout[row])
            g.in_ok[vid] = bytearray(min_[row])
            g.edge_counts[vid] = sum(ok)
        return g

    # -- queries ------------------------------------------------------------

    @property
    def node_count(self) -> int:
        return len(self.node_coords)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    def node(self, node_idx: int) -> tuple[float, float]:
        c = self.node_coords[node_idx]
        return (float(c[0]), float(c[1]))

    def node_fraction(self, seg: dict, node_idx: int) -> float | None:
        for n, at in seg["conns"]:
            if n == node_idx:
                return at
        return None

    def edge_geometry(self, edge_idx: int) -> list[list[float]]:
        """Coordinates along the directed edge: [start, ..., end]."""
        a, b, seg_idx, heading, *_ = self.edges[edge_idx]
        seg = self.segments[seg_idx]
        t_a = self.node_fraction(seg, a)
        t_b = self.node_fraction(seg, b)
        if t_a is None or t_b is None:
            return [list(self.node(a)), list(self.node(b))]
        coords, cums, total = seg["coords"], seg["cums"], seg["length"]
        lo, hi = (t_a, t_b) if t_a <= t_b else (t_b, t_a)
        geom = subline(coords, cums, total, lo, hi)
        if heading == 1:
            # traversal runs opposite to the stored geometry
            geom.reverse()
        return geom

    def edge_columns(self) -> tuple:
        """(frm, to, seg, head, length, cat) edge columns as memoryviews.

        Fast scalar access for the routing hot loop: memoryview[i] yields a
        plain Python int/float without building a row tuple. Column identity
        matches the canonical edge tuple fields 0-5.
        """
        if self._edge_cols is None:
            if isinstance(self.edges, EdgeTable):
                arrays = (
                    self.edges.frm,
                    self.edges.to,
                    self.edges.seg,
                    self.edges.head,
                    self.edges.length,
                    self.edges.cat,
                )
            else:
                n = len(self.edges)
                cat_idx = {c: i for i, c in enumerate(config.CATEGORIES)}
                arrays = (
                    np.fromiter((e[0] for e in self.edges), dtype=np.int32, count=n),
                    np.fromiter((e[1] for e in self.edges), dtype=np.int32, count=n),
                    np.fromiter((e[2] for e in self.edges), dtype=np.int32, count=n),
                    np.fromiter((e[3] for e in self.edges), dtype=np.uint8, count=n),
                    np.fromiter((e[4] for e in self.edges), dtype=np.float64, count=n),
                    np.fromiter((cat_idx[e[5]] for e in self.edges), dtype=np.uint8, count=n),
                )
            self._edge_cols = tuple(memoryview(np.ascontiguousarray(a)) for a in arrays)
        return self._edge_cols

    def outgoing(self, node_idx: int) -> list[int]:
        return self.adj[node_idx]


class EdgeTable(Sequence):
    """Columnar edge storage used by load_bundle().

    Same contract as the list of 8-tuples that build() produces —
    `edges[i]` returns the canonical (from_node, to_node, seg_idx, heading,
    length_m, category, effective_speed_mph, posted_speed_mph_or_None) tuple —
    at a fraction of the memory (277k rows: ~10 MB vs ~70 MB of tuples).
    """

    __slots__ = ("frm", "to", "seg", "head", "length", "cat", "eff", "posted")

    def __init__(self, frm, to, seg, head, length, cat, eff, posted):
        self.frm = frm
        self.to = to
        self.seg = seg
        self.head = head
        self.length = length
        self.cat = cat
        self.eff = eff
        self.posted = posted

    def __len__(self) -> int:
        return len(self.frm)

    def __getitem__(self, i: int) -> tuple:
        p = self.posted[i]
        return (
            self.frm[i],
            self.to[i],
            self.seg[i],
            self.head[i],
            self.length[i],
            config.CATEGORIES[self.cat[i]],
            self.eff[i],
            None if p != p else p,  # NaN encodes "no posted limit"
        )

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def __eq__(self, other) -> bool:
        return list(self) == list(other)

    def __hash__(self):
        return id(self)


def _plain(pt) -> list[float]:
    return [float(pt[0]), float(pt[1])]


def subline(coords, cums, total: float, t0: float, t1: float) -> list[list[float]]:
    """Extract the sub-polyline of `coords` between fractions t0 and t1 (0..1).

    Always returns at least the start and end point; consecutive duplicates
    are removed. Points are plain floats (coords may be a numpy array).
    """
    if total <= 0 or t1 <= t0:
        return []
    d0, d1 = t0 * total, t1 * total
    out = [_plain(geo.point_on_linestring([tuple(c) for c in coords], t0))]
    for i in range(len(coords) - 1):
        c0, c1 = cums[i], cums[i + 1]
        if c0 > d0 and c0 < d1:
            out.append(_plain(coords[i]))
        if c1 > d0 and c1 < d1:
            out.append(_plain(coords[i + 1]))
    end = _plain(geo.point_on_linestring([tuple(c) for c in coords], t1))
    if out[-1] != end:
        out.append(end)
    return out
