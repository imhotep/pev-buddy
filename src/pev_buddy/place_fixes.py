"""Known-bad place records in the Overture extract, dropped from place search.

Overture sometimes puts a place far from where it is, typically with no
address. A general rule ("drop an address-less place far from same-named
ones") was tried and rejected: having an address doesn't make a record right
(the addressed "Fisherman's Wharf" and "Bay Bridge" rows are unrelated
businesses), so it would drop correct landmarks. This list holds only
records checked by hand against the 2026-09-23.0 release, with why each is
wrong; re-check it when the Overture release changes.
"""

from __future__ import annotations

from .text import fold

# name (matched case- and accent-insensitively), category (None: any), why.
BAD_PLACES: tuple[tuple[str, str | None, str], ...] = (
    (
        "Ferry Building, Embarcadero",
        "historic_site",
        "placed in Bernal Heights (37.736, -122.416); the Ferry Building is on the Embarcadero, "
        "where the other Ferry Building records correctly are (issue #24)",
    ),
    (
        "Oakland Coliseum",
        None,
        "both records sit inside SF (Excelsior 37.722, -122.437; SoMa 37.778, -122.415); "
        "the Coliseum is in Oakland (issue #36)",
    ),
    (
        "Oakland Colesium",
        None,
        "a misspelled Oakland Coliseum placed in SoMa (37.787, -122.404)",
    ),
    (
        "Oakland Oracle Arena",
        "stadium_arena",
        "Oracle Arena is in Oakland; this record is at 511 Harrison St in SoMa (37.786, -122.393)",
    ),
    (
        "SFO International Airport",
        None,
        "both records are misplaced (Brisbane 37.666, -122.425; Excelsior 37.722, -122.447); "
        "SFO is south of the city, outside the routable area",
    ),
)

_BAD = {(fold(name), cat) for name, cat, _why in BAD_PLACES}


def is_bad_place(name: str, category: str | None) -> bool:
    n = fold(name)
    return (n, None) in _BAD or (n, category) in _BAD
