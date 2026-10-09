"""Turn-by-turn step generation from a routed edge sequence."""

from __future__ import annotations

from . import config, geo
from .graph import RoadGraph

STRAIGHT_DEG = 25.0  # |turn| below this is "continue"
SLIGHT_DEG = 60.0  # |turn| below this is a slight turn
SHARP_DEG = 150.0  # |turn| above this is a U-turn
MIN_STEP_M = 40.0  # merge steps shorter than this into the previous one
SHORT_UNNAMED_M = 60.0  # unnamed runs shorter than this never get their own step


def build_steps(
    graph: RoadGraph,
    edge_idxs: list[int],
    destination_name: str | None = None,
    vehicle: str = config.DEFAULT_VEHICLE,
) -> list[dict]:
    """Build human-readable steps. Each step:
    {index, maneuver, instruction, road, distance_m, duration_s, turn_point, bearing_after, geometry}
    `geometry` is the [lon, lat] polyline the step travels (single point for
    the zero-length arrival step), used for on-map highlighting.
    """
    arrival = _sentence(f"Arrive at {destination_name or 'your destination'}")
    if not edge_idxs:
        return [
            {
                "index": 1,
                "maneuver": "arrive",
                "instruction": arrival,
                "road": None,
                "distance_m": 0.0,
                "duration_s": 0.0,
                "turn_point": None,
                "bearing_after": None,
                "geometry": None,
            }
        ]

    geoms = [graph.edge_geometry(e) for e in edge_idxs]
    lengths = [graph.edges[e][4] for e in edge_idxs]
    veh = config.VEHICLE_TYPES[vehicle]
    # Per-edge travel time (vehicle cap x road limit), summed per step.
    durations = [
        config.edge_duration_s(veh, graph.edges[e][4], graph.edges[e][5], graph.edges[e][6]) for e in edge_idxs
    ]
    segs = [graph.edges[e][2] for e in edge_idxs]
    names = [graph.segments[s]["name"] for s in segs]

    def inbound_bearing(i: int) -> float:
        g = geoms[i]
        if len(g) < 2:
            return 0.0
        a, b = g[-2], g[-1]
        return geo.bearing(a[0], a[1], b[0], b[1])

    def outbound_bearing(i: int) -> float:
        g = geoms[i]
        if len(g) < 2:
            return 0.0
        a, b = g[0], g[1]
        return geo.bearing(a[0], a[1], b[0], b[1])

    # --- pass 1: group consecutive edges into road groups -------------------
    # A group is a contiguous run of edges along one physical road. Grouping
    # before any length-based merging keeps turns honest: Overture splits
    # streets at every crossing, so the first piece of a new street is often a
    # short stub that must belong to the *new* road, not the old one.
    #
    # Unnamed runs are handled by length: an unnamed tail after a named road
    # (or a stub leading into a named road) folds in only while short; a
    # long unnamed road keeps its own group (see pass 2 for the rest).
    groups: list[dict] = []
    for i, e in enumerate(edge_idxs):
        if i == 0:
            groups.append(
                {
                    "start": 0,
                    "end": 0,
                    "angle": 0.0,
                    "len": lengths[0],
                    "name": names[0],
                    "named": names[0] is not None,
                    "tail": 0.0 if names[0] is not None else lengths[0],
                    "from_node": graph.edges[e][0],
                }
            )
            continue
        ang = geo.turn_angle(inbound_bearing(i - 1), outbound_bearing(i))
        g = groups[-1]
        cur, prev = names[i], names[i - 1]
        merge = False
        if segs[i] == segs[i - 1]:
            merge = True  # one physical segment: one group, however it bends
        elif abs(ang) <= STRAIGHT_DEG:
            if cur is not None and prev is not None:
                merge = cur == prev
            elif cur is None:
                if g["named"]:
                    merge = g["tail"] + lengths[i] < SHORT_UNNAMED_M  # short unnamed tail
                else:
                    merge = True  # continuing an already-unnamed road
            else:
                merge = g["tail"] < SHORT_UNNAMED_M  # short stub leading into a named road
        if merge:
            g["end"] = i
            g["len"] += lengths[i]
            if cur is None:
                g["tail"] += lengths[i]
            else:
                g["tail"] = 0.0
                if g["name"] is None:
                    g["name"] = cur
                    g["named"] = True
        else:
            groups.append(
                {
                    "start": i,
                    "end": i,
                    "angle": ang,
                    "len": lengths[i],
                    "name": cur,
                    "named": cur is not None,
                    "tail": 0.0 if cur is not None else lengths[i],
                    "from_node": graph.edges[e][0],
                }
            )

    # --- pass 2: fold unnamed runs into the roads around them ----------------
    # Overture leaves many bike-lane pieces and short connectors unnamed; on
    # their own they fragment a street into "Turn onto the street" steps.
    #
    # (a) An unnamed run between two runs of the same street, with no real
    #     turn into or out of it and the same heading on both sides, is part of
    #     that street (a bike-lane piece, a jog around a median).
    k = 1
    while k < len(groups) - 1:
        before, gap, after = groups[k - 1 : k + 2]
        net = geo.turn_angle(inbound_bearing(before["end"]), outbound_bearing(after["start"]))
        if (
            gap["name"] is None
            and before["name"] is not None
            and before["name"] == after["name"]
            and abs(gap["angle"]) < SLIGHT_DEG
            and abs(after["angle"]) < SLIGHT_DEG
            and abs(net) <= STRAIGHT_DEG
        ):
            before["end"] = after["end"]
            before["len"] += gap["len"] + after["len"]
            del groups[k : k + 2]
        else:
            k += 1

    # (b) A short unnamed run (a crosswalk, a curb cut, a slip lane) gets no
    #     step of its own: it joins the previous road and the next road's step
    #     keeps its own turn. (The run's own entry angle is no guide: these
    #     pieces zigzag.) A route that starts on one departs along the road it
    #     leads to. The destination's group is left alone, so a route keeps
    #     its depart and final steps.
    k = 0
    while k < len(groups) - 1:
        grp = groups[k]
        if grp["name"] is not None or grp["len"] >= SHORT_UNNAMED_M or (k == 0 and len(groups) == 2):
            k += 1
            continue
        if k > 0:
            groups[k - 1]["end"] = grp["end"]
            groups[k - 1]["len"] += grp["len"]
        else:
            nxt = groups[1]
            nxt.update(start=0, angle=0.0, from_node=grp["from_node"], len=grp["len"] + nxt["len"])
        del groups[k]

    # The final group is a real turn onto the destination's street: keep it as
    # a turn step and follow it with a zero-length arrival.
    last_turn = len(groups) > 1 and abs(groups[-1]["angle"]) > STRAIGHT_DEG

    def signpost_label_for(i0: int) -> str | None:
        # Transition happens at the group's start node, from the previous
        # edge's segment into this group's first segment.
        if i0 == 0:
            return None
        return _signpost_label(
            graph, segs[i0 - 1], segs[i0], graph.edges[edge_idxs[i0]][0], graph.edges[edge_idxs[i0]][3]
        )

    def unnamed_noun(i0: int, i1: int) -> str | None:
        # What an unnamed step travels on, judged by its longest edge.
        k = max(range(i0, i1 + 1), key=lengths.__getitem__)
        return _unnamed_noun(graph.segments[segs[k]]["class"], graph.edges[edge_idxs[k]][5])

    # --- pass 3: steps --------------------------------------------------------
    steps: list[dict] = []
    for j, grp in enumerate(groups):
        i0, i1 = grp["start"], grp["end"]
        is_last = j == len(groups) - 1
        # A short road piece (whole group, not just its first edge) that is not
        # sharp and not the destination folds into the previous step.
        if j > 0 and not is_last and grp["len"] < MIN_STEP_M and abs(grp["angle"]) <= SHARP_DEG:
            steps[-1]["i1"] = i1
            continue

        road = grp["name"]
        if j == 0:
            maneuver = "depart"
            # Overall heading along the road being named: the group's ends may
            # be short unnamed pieces folded in above, pointing anywhere.
            on_road = [k for k in range(i0, i1 + 1) if names[k] == road]
            (lon0, lat0), (lon1, lat1) = geoms[on_road[0]][0], geoms[on_road[-1]][-1]
            instruction = _depart_instruction(geo.bearing(lon0, lat0, lon1, lat1), road or unnamed_noun(i0, i1))
        elif is_last and not last_turn:
            maneuver = "arrive"
            instruction = arrival
        else:
            maneuver, phrase = _classify(grp["angle"])
            label = signpost_label_for(i0)
            # The road keeps its name across this bend (Overture splits curved
            # streets into separate segments): don't say "onto" again. Only
            # when the rider is already on it, i.e. it is the previous step's.
            if road and road == steps[-1]["road"] and maneuver != "uturn" and not label:
                direction = "right" if grp["angle"] > 0 else "left"
                bend = "Bear slightly" if maneuver in ("slight_left", "slight_right") else "Turn"
                instruction = _sentence(f"{bend} {direction}, staying on {road}")
            else:
                instruction = _turn_instruction(maneuver, phrase, road or unnamed_noun(i0, i1), label)

        steps.append(
            {
                "i0": i0,
                "i1": i1,
                "maneuver": maneuver,
                "instruction": instruction,
                "road": road,
                "from_node": grp["from_node"],
            }
        )

    if last_turn:
        steps.append(
            {
                "i0": len(edge_idxs) - 1,
                "i1": len(edge_idxs) - 1,
                "maneuver": "arrive",
                "instruction": arrival,
                "road": groups[-1]["name"],
                "from_node": graph.edges[edge_idxs[-1]][1],
                "zero": True,
            }
        )

    def chunk_geometry(i0: int, i1: int) -> list[list[float]]:
        coords: list[list[float]] = []
        for k in range(i0, i1 + 1):
            for c in geoms[k]:
                pt = [round(float(c[0]), 6), round(float(c[1]), 6)]
                if not coords or coords[-1] != pt:
                    coords.append(pt)
        return coords

    out: list[dict] = []
    for j, st in enumerate(steps):
        i0, i1 = st["i0"], st["i1"]
        dist = 0.0 if st.get("zero") else sum(lengths[i0 : i1 + 1])
        dur = 0.0 if st.get("zero") else sum(durations[i0 : i1 + 1])
        # Geometry of the segment this step covers (for map highlighting).
        geometry = [list(graph.node(st["from_node"]))] if st.get("zero") else chunk_geometry(i0, i1)
        out.append(
            {
                "index": j + 1,
                "maneuver": st["maneuver"],
                "instruction": st["instruction"],
                "road": st["road"],
                "distance_m": dist,
                "duration_s": dur,
                "turn_point": [float(c) for c in graph.node(st["from_node"])] if j > 0 else None,
                "bearing_after": outbound_bearing(i1),
                "geometry": geometry,
            }
        )
    return out


def _sentence(text: str) -> str:
    """End `text` with a period unless it already ends in sentence punctuation
    (street names can: "Herb Caen Way..." must not become "Way....")."""
    return text if text.endswith((".", "!", "?", "…")) else f"{text}."


# Unnamed ways are described by what they are, never as "the street".
_PATH_CLASSES = frozenset({"footway", "path", "track", "pedestrian"})


def _unnamed_noun(seg_class: str, category: str) -> str | None:
    if seg_class == "cycleway":
        return "the bike path"
    if seg_class == "footway" and category == "sidewalk":
        return "the sidewalk"
    if seg_class in _PATH_CLASSES:
        return "the path"
    return None  # an unnamed street or connector: just say which way to turn


def _depart_instruction(bearing: float, way: str | None) -> str:
    if way:
        return _sentence(f"Head {geo.cardinal(bearing)} on {way}")
    return f"Head {geo.cardinal(bearing)} to begin."


def _classify(ang: float) -> tuple[str, str]:
    """(maneuver, phrase) for a turn angle, e.g. ('slight_left', 'Turn slightly left')."""
    a = abs(ang)
    if a <= STRAIGHT_DEG:
        return "straight", "Continue"
    if a >= SHARP_DEG:
        return "uturn", "Make a U-turn"
    side = "right" if ang > 0 else "left"  # positive = right turn (see geo.turn_angle)
    if a < SLIGHT_DEG:
        return f"slight_{side}", f"Turn slightly {side}"
    return side, f"Turn {side}"


def _turn_instruction(maneuver: str, phrase: str, way: str | None, label: str | None = None) -> str:
    """E.g. 'Turn left onto Main Street toward Downtown.' The way and the
    signpost label are each optional: 'Turn left.'"""
    text = phrase
    if way:
        text += f" {'on' if maneuver == 'straight' else 'onto'} {way}"
    if label:
        text += f" toward {label}"
    return _sentence(text)


def _signpost_label(
    graph: RoadGraph, from_seg: int, to_seg: int, at_node: int, to_heading: int
) -> str | None:
    """Overture signpost label for this transition, if the source segment has one."""
    mask = 1 if to_heading == 0 else 2
    for entry in graph.dest.get(from_seg, []):
        to_seg_idx, to_node, label, hm = entry
        if to_seg_idx == to_seg and to_node == at_node and (hm & mask) and label:
            return label
    return None
