"""Unit tests for geographic helpers."""

import re

from pev_buddy.geo import format_distance_imperial

_METRE = re.compile(r"(^|\s)\d+(\.\d+)?\s*m(\s|$|[.,;:])")


def test_format_distance_imperial_feet_and_miles():
    assert format_distance_imperial(60) == "197 ft"  # snap-style short distance
    assert format_distance_imperial(0) == "0 ft"
    assert format_distance_imperial(160.934) == "0.1 mi"  # boundary at 0.1 mi
    assert format_distance_imperial(3999) == "2.5 mi"
    for meters in (60, 3999, 200, 10):
        label = format_distance_imperial(meters)
        assert _METRE.search(label) is None, label
        assert label.endswith(" ft") or label.endswith(" mi")
