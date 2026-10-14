"""List place records that are probably misplaced, for a person to review.

    python -m pev_buddy.place_audit [data_dir]

Overture sometimes puts a place far from where it is (QA found the Ferry
Building historic site in Bernal Heights, Lake Merritt on Fillmore St). The
reliable tell this script uses: a record with no address, inside the city,
whose name names a place outside it ("Lake Merritt Oakland", "Vista Point
Marin County"). That rule alone is not safe to apply automatically: Muni's
"San Jose and Ocean station" is on San Jose Avenue in SF, and "Daly City
BART station" sits on the city line. So this only prints candidates; the
ones confirmed wrong go into place_fixes.BAD_PLACES with the reason.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import orjson

from . import config
from .place_fixes import is_bad_place

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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    data_dir = Path(argv[0]) if argv else config.DATA_DIR
    rows = orjson.loads((data_dir / "places.json").read_bytes())
    found = suspects(rows)
    print(f"{len(found)} place records to review (no address, inside SF, named after somewhere outside it):")
    for _id, name, cat, _addr, lon, lat in (r[:6] for r in found):
        print(f"  {name}  [{cat}]  {lat:.4f}, {lon:.4f}")
    print("Add the confirmed-wrong ones to src/pev_buddy/place_fixes.py with the reason.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
