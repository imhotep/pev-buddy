"""List place records that are probably misplaced, for a person to review.

    python -m pev_buddy.place_audit [data_dir]

Overture sometimes puts a place far from where it is (QA found the Ferry
Building historic site in Bernal Heights, Lake Merritt on Fillmore St). The
reliable tell this script uses: a record with no address, inside the city,
whose name names a place outside it ("Lake Merritt Oakland", "Vista Point
Marin County"). That rule alone is not safe to apply automatically: Muni's
"San Jose and Ocean station" is on San Jose Avenue in SF, and "Daly City
BART station" sits on the city line.

A second tell (#56, "Fort Mason" in North Beach): a record with no address
far from the tight cluster of records named after it ("Fort Mason Center",
"Fort Mason Chapel", ...). A chain's branches are spread out, not clustered,
so they don't count as a cluster.

Neither rule is safe to apply automatically, so this only prints
candidates; the ones confirmed wrong go into place_fixes.BAD_PLACES with
the reason.
"""

from __future__ import annotations

import re
import statistics
import sys
from bisect import bisect_left
from pathlib import Path

import orjson

from . import config, geo
from .place_fixes import is_bad_place
from .text import fold

# Places outside the city that names tend to mention.
OUTSIDE_PLACES = re.compile(
    r"\b(Oakland|Berkeley|Alameda|Emeryville|Richmond|Sausalito|Saucalito|Marin|Tiburon|Mill Valley|"
    r"Daly City|Pacifica|Brisbane|San Mateo|Burlingame|Millbrae|SFO|Palo Alto|San Jose|Walnut Creek|"
    r"Lake Merritt)\b",
    re.IGNORECASE,
)


def suspects(rows: list) -> list[list]:
    """Rows [id, name, category, address, lon, lat] worth a look: no address,
    inside the city, named after somewhere outside it, not already listed."""
    out = []
    for r in rows:
        _id, name, cat, address, lon, lat = r[:6]
        if address or not config.in_service_area(lon, lat):
            continue
        if not OUTSIDE_PLACES.search(name or "") or is_bad_place(name, cat):
            continue
        out.append(r)
    return out


NAMESAKE_MIN = 3  # records named after a place, needed to call them a cluster
CLUSTER_M = 400.0  # their median distance from the cluster's center, at most
FAR_M = 1000.0  # a record this far from that center is worth a look


def far_from_namesakes(rows: list) -> list[tuple[list, float]]:
    """(row, meters) for address-less records inside the city that sit far
    from the tight cluster of records named after them ("Fort Mason" vs
    "Fort Mason Center", "Fort Mason Chapel", ...), not already listed."""
    named = sorted(((fold(r[1] or ""), i) for i, r in enumerate(rows)), key=lambda t: t[0])
    keys = [k for k, _ in named]
    out = []
    for r in rows:
        _id, name, cat, address, lon, lat = r[:6]
        if address or not name or not config.in_service_area(lon, lat) or is_bad_place(name, cat):
            continue
        key = fold(name)
        lo = bisect_left(keys, key + " ")
        hi = bisect_left(keys, key + " \uffff")
        sakes = [rows[i] for _k, i in named[lo:hi]]
        if len(sakes) < NAMESAKE_MIN:
            continue
        clon = statistics.median(s[4] for s in sakes)
        clat = statistics.median(s[5] for s in sakes)
        spread = statistics.median(geo.haversine(s[4], s[5], clon, clat) for s in sakes)
        d = geo.haversine(lon, lat, clon, clat)
        if spread <= CLUSTER_M and d >= FAR_M:
            out.append((r, d))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    data_dir = Path(argv[0]) if argv else config.DATA_DIR
    rows = orjson.loads((data_dir / "places.json").read_bytes())
    found = suspects(rows)
    print(f"{len(found)} place records to review (no address, inside SF, named after somewhere outside it):")
    for _id, name, cat, _addr, lon, lat in (r[:6] for r in found):
        print(f"  {name}  [{cat}]  {lat:.4f}, {lon:.4f}")
    far = far_from_namesakes(rows)
    print(f"{len(far)} place records to review (no address, far from the places named after them):")
    for r, d in far:
        _id, name, cat, _addr, lon, lat = r[:6]
        print(f"  {name}  [{cat}]  {lat:.4f}, {lon:.4f}  ({d / 1000:.1f} km away)")
    print("Add the confirmed-wrong ones to src/pev_buddy/place_fixes.py with the reason.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
