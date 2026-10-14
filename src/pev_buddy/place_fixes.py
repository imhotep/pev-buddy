"""Known-bad place records in the Overture extract, dropped from place search.

Overture sometimes puts a place far from where it is, typically with no
address. A general rule ("drop an address-less place far from same-named
ones") was tried and rejected: having an address doesn't make a record right
(the addressed "Fisherman's Wharf" and "Bay Bridge" rows are unrelated
businesses), so it would drop correct landmarks. This list holds only
records checked by hand against the 2026-09-23.0 release, with why each is
wrong; re-check it when the Overture release changes. `python -m
pev_buddy.place_audit` lists likely candidates for that review (see there).
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
        "SFO Int'l Airport",
        "airport",
        "a second SFO airport record, placed in the Mission (37.758, -122.423) (issue #42)",
    ),
    (
        "Lake Merritt Oakland",
        "lake",
        "Lake Merritt is in Oakland; this record is near Fillmore St (37.786, -122.433) (issue #43)",
    ),
    (
        "University Of California Berkeley",
        "college_university",
        "UC Berkeley is in Berkeley; this record is in SoMa (37.778, -122.413)",
    ),
    (
        "Coyote Point Park, San Mateo",
        "park",
        "Coyote Point is in San Mateo; this record is in Glen Park (37.732, -122.448)",
    ),
    (
        "Vista Point Marin County",
        "mountain",
        "the Golden Gate Bridge's Vista Point is in Marin; this record is in Pacific Heights (37.800, -122.435)",
    ),
    (
        "Saucalito City, Marin County",
        "community_center",
        "Sausalito is in Marin; this record is in Pacific Heights (37.797, -122.436)",
    ),
    (
        "Sutro Baths and Cliff House",
        "historic_site",
        "Sutro Baths and the Cliff House are at Lands End (37.780, -122.514); this record is in "
        "Golden Gate Park (37.771, -122.468)",
    ),
    (
        "Lands End/Sutro Baths",
        "national_park",
        "Lands End is at the city's northwest corner; this record is near the Panhandle (37.775, -122.441)",
    ),
    (
        "Terra Nova High School (Pacifica)",
        "high_school",
        "the school is in Pacifica; this record is in the Excelsior (37.708, -122.437)",
    ),
    (
        "Sharp Park Beach, Pacifica, California",
        "beach",
        "Sharp Park Beach is in Pacifica; this record is in the Excelsior (37.720, -122.445)",
    ),
    (
        "Nike Missile Site- Marin Headlands",
        "sports_and_recreation",
        "the Nike site is in the Marin Headlands; this record is in the Marina (37.806, -122.438)",
    ),
    (
        "Marin Headlands, California",
        "mountain",
        "the Marin Headlands are across the Golden Gate; this record is in the Presidio (37.802, -122.464)",
    ),
    (
        "Fish Restaurant, Sausalito",
        "restaurant",
        "the restaurant is on the Sausalito waterfront; this record is near Union Square (37.789, -122.409)",
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
