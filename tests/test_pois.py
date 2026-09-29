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
