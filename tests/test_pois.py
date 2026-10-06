import orjson

from pev_buddy.pois import POIIndex, format_category

ROWS = [
    ["p-1", "Tartine Bakery", "restaurant", "3457 Fillmore St", -122.43, 37.79],
    ["p-2", "Tartine Manufactory", "cafe", "1234 24th St", -122.43, 37.76],
    ["p-3", "Cable Car Bar", "bar", "22 Bay St", -122.42, 37.80],
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


def test_search_includes_address(tmp_path):
    """Issue #2: place hits expose the street address for UI subtitles."""
    idx = POIIndex(make(tmp_path))
    r = idx.search("tartine bakery")
    assert r[0]["address"] == "3457 Fillmore St"


def test_same_name_near_duplicates_collapsed(tmp_path):
    """Issue #2: identical-name POIs within DEDUPE_M collapse to one hit.
    Prefer the row that carries an address (better subtitle)."""
    from pev_buddy.pois import DEDUPE_M

    # Two "Ferry Building" pins ~20 m apart (well under DEDUPE_M); one lacks address.
    lon, lat = -122.3936, 37.7955
    rows = [
        ["fb-1", "Ferry Building", "landmark", "", lon, lat],
        ["fb-2", "Ferry Building", "marketplace", "1 Ferry Building", lon + 0.00015, lat],
        ["fb-far", "Ferry Building", "historic_site", "Far Side St", lon + 0.01, lat - 0.01],  # ~1.4 km
    ]
    idx = POIIndex(make(tmp_path, rows))
    hits = idx.search("ferry building")
    near = [h for h in hits if h["name"] == "Ferry Building"]
    # Near pair collapses to one; far peer kept (different location).
    assert len(near) == 2
    assert any(h["address"] == "1 Ferry Building" for h in near)
    # The kept near hit should be the one with an address.
    near_with_addr = [h for h in near if h["address"] == "1 Ferry Building"]
    assert len(near_with_addr) == 1
    assert DEDUPE_M == 75.0


def test_different_names_not_deduped_by_proximity(tmp_path):
    """Nearby places with different names must both survive (distinguish via subtitle)."""
    lon, lat = -122.3936, 37.7955
    rows = [
        ["a", "Ferry Building Marketplace", "shop", "1 Ferry Building", lon, lat],
        ["b", "Ferry Building B2B", "office", "1 Ferry Building Ste 2", lon + 0.0001, lat],
    ]
    idx = POIIndex(make(tmp_path, rows))
    hits = idx.search("ferry building")
    names = {h["name"] for h in hits}
    assert "Ferry Building Marketplace" in names
    assert "Ferry Building B2B" in names
