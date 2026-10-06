import orjson

from pev_buddy.pois import POIIndex, format_category, suppress_coordinate_outliers

ROWS = [
    ["p-1", "Tartine Bakery", "restaurant", "3457 Fillmore St", -122.43, 37.79],
    ["p-2", "Tartine Manufactory", "cafe", "1234 24th St", -122.43, 37.76],
    ["p-3", "Cable Car Bar", "bar", "22 Bay St", -122.42, 37.80],
]

# Real coords from production /api/search?q=Ferry%20Building (issue #1).
# The Historic Site row is ~7 km south in Bernal Heights; peers cluster on
# the Embarcadero around (-122.3936, 37.7955).
FERRY_ROWS = [
    ["fb-1", "Ferry Building", "b2b_service", "1 Ferry Building", -122.39361, 37.79544],
    ["fb-2", "Ferry Building Gate B", "pier", "Ferry Building", -122.39391, 37.79641],
    ["fb-3", "Ferry Building Line", "sport_or_fitness_facility", "1 Ferry Plz", -122.39515, 37.79375],
    ["fb-4", "Ferry Building, Embarcadero", "historic_site", None, -122.41619, 37.73639],
    ["fb-5", "End of the Pier Behind the Ferry Building", "bridge", "Ferry Plaza", -122.39128, 37.79575],
    ["fb-6", "glassybaby ferry building", "flowers_and_gifts_store", "1 Ferry Building, Ste K8", -122.39334, 37.79556],
    ["fb-7", "Gott's SF Ferry Building", "restaurant", "Ferry Building, Ferry Plz Bldg #6", -122.39402, 37.79583],
    ["fb-8", "Red Bay Coffee Ferry Building", "coffee_shop", "Ferry Building, 1", -122.39306, 37.79535],
]


def make(tmp_path, rows=None):
    p = tmp_path / "places.json"
    p.write_bytes(orjson.dumps(rows if rows is not None else ROWS))
    return p


def test_exact_match(tmp_path):
    idx = POIIndex(make(tmp_path))
    r = idx.search("tartine bakery")
    assert len(r) == 1
    assert r[0]["name"] == "Tartine Bakery"
    assert r[0]["category"] == "Restaurant"
    assert r[0]["lon"] == -122.43


def test_prefix_beats_substring(tmp_path):
    idx = POIIndex(make(tmp_path))
    r = idx.search("tartine")
    names = [x["name"] for x in r]
    assert names[0] == "Tartine Bakery"
    assert "Tartine Manufactory" in names


def test_substring(tmp_path):
    idx = POIIndex(make(tmp_path))
    r = idx.search("cable")
    assert [x["name"] for x in r] == ["Cable Car Bar"]


def test_case_insensitive(tmp_path):
    idx = POIIndex(make(tmp_path))
    assert idx.search("TARTINE BAKERY")[0]["name"] == "Tartine Bakery"


def test_no_matches(tmp_path):
    idx = POIIndex(make(tmp_path))
    assert idx.search("") == []
    assert idx.search("zzz") == []


def test_limit(tmp_path):
    idx = POIIndex(make(tmp_path))
    assert len(idx.search("t", limit=2)) == 2


def test_format_category():
    assert format_category("casual_eatery") == "Casual Eatery"
    assert format_category(None) == "Place"


def test_suppress_ferry_building_historic_site_outlier():
    """Bernal Heights 'Ferry Building, Embarcadero' is dropped; Embarcadero peers kept."""
    results = [
        {"name": r[1], "lon": r[4], "lat": r[5], "score": 80}
        for r in FERRY_ROWS
    ]
    kept = suppress_coordinate_outliers(results)
    names = {r["name"] for r in kept}
    assert "Ferry Building, Embarcadero" not in names
    assert "Ferry Building" in names
    assert "Ferry Building Gate B" in names
    assert len(kept) == len(results) - 1


def test_suppress_leaves_spread_out_places_alone():
    """Legitimate same-query places that don't form a dense cluster stay."""
    results = [
        {"name": "Cable Car Museum", "lon": -122.4117, "lat": 37.7947, "score": 80},
        {"name": "Cable Car Turnaround", "lon": -122.4079, "lat": 37.7849, "score": 60},
        {"name": "Cable Car Barn", "lon": -122.4215, "lat": 37.8054, "score": 60},
    ]
    assert suppress_coordinate_outliers(results) == results


def test_suppress_noop_with_too_few_peers():
    results = [
        {"name": "A", "lon": -122.39, "lat": 37.79, "score": 100},
        {"name": "B", "lon": -122.42, "lat": 37.74, "score": 80},
    ]
    assert suppress_coordinate_outliers(results) == results


def test_ferry_building_search_hides_bernal_outlier(tmp_path):
    idx = POIIndex(make(tmp_path, FERRY_ROWS))
    r = idx.search("Ferry Building", limit=10)
    names = [x["name"] for x in r]
    assert "Ferry Building, Embarcadero" not in names
    assert names[0] == "Ferry Building"
    # Remaining hits stay near the Embarcadero, not Bernal Heights.
    assert all(abs(x["lat"] - 37.795) < 0.02 for x in r)
