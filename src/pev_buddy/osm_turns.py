"""Turn restrictions that exempt bicycles, from OpenStreetMap.

Overture carries OSM's turn restrictions as `prohibited_transitions` but drops
their `except` tag, so a restriction meant for cars only binds every vehicle.
On Mission St the Muni red-lane rule "only right turn at 16th, except
bicycles, buses and taxis" became "no straight on" for e-scooters too, and
routes detoured around the block. This step pulls the bicycle-exempt
restrictions for the bbox from the Overpass API; the graph build then lifts
the matching Overture prohibitions for bicycle-mode vehicles (mopeds, which
route as motorcycles, still obey them).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)


def _query(bbox: tuple[float, float, float, float]) -> str:
    xmin, ymin, xmax, ymax = bbox
    return (
        "[out:json][timeout:90];"
        f'rel({ymin},{xmin},{ymax},{xmax})[type=restriction][except~"bicycle"]->.r;'
        ".r out body;"
        "way(r.r);out tags;"
        'node(r.r:"via");out skel qt;'
    )


def _post(url: str, query: str) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(url, data, {"User-Agent": "pev-buddy (sync)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def parse(payload: dict) -> list[dict]:
    """Overpass JSON -> [{id, restriction, via: [lon, lat], from: [names], to: [names]}].

    Restrictions whose `via` is a way (not a node) are skipped: Overture's
    transitions are keyed on a single connector.
    """
    els = payload.get("elements") or []
    names = {e["id"]: (e.get("tags") or {}).get("name") for e in els if e["type"] == "way"}
    nodes = {e["id"]: [e["lon"], e["lat"]] for e in els if e["type"] == "node"}
    out = []
    for e in els:
        if e["type"] != "relation":
            continue
        tags = e.get("tags") or {}
        if "bicycle" not in (tags.get("except") or "").split(";"):
            continue
        roles: dict[str, list] = {"from": [], "to": [], "via": []}
        for m in e.get("members") or []:
            if m["role"] in roles:
                roles[m["role"]].append(m)
        vias = [m for m in roles["via"] if m["type"] == "node" and m["ref"] in nodes]
        if len(vias) != 1:
            continue
        out.append(
            {
                "id": e["id"],
                "restriction": tags.get("restriction") or "",
                "via": nodes[vias[0]["ref"]],
                "from": sorted({names.get(m["ref"]) for m in roles["from"]} - {None}),
                "to": sorted({names.get(m["ref"]) for m in roles["to"]} - {None}),
            }
        )
    return out


def extract(bbox: tuple[float, float, float, float], out_path: Path, post=None) -> int:
    """Fetch and write the bicycle-exempt restrictions; returns how many.

    Tries each Overpass mirror in turn and raises the last error if none
    answers (the sync step keeps any existing file then).
    """
    post = post or _post
    q = _query(bbox)
    err: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            rows = parse(post(url, q))
        except Exception as e:  # busy mirror, timeout, HTML error page
            err = e
            continue
        Path(out_path).write_text(json.dumps(rows, indent=1))
        return len(rows)
    raise RuntimeError(f"no Overpass mirror answered ({err})")


def load(path: Path) -> list[dict]:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else []
