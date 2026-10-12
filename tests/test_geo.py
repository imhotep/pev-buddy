import pytest

from pev_buddy.geo import format_distance


@pytest.mark.parametrize(
    "meters, expected",
    [
        (0, "0 ft"),
        (12, "40 ft"),
        (60, "200 ft"),
        (160, "520 ft"),  # just under a tenth of a mile: still feet
        (161, "0.1 mi"),
        (1609.344, "1.0 mi"),
        (3999, "2.5 mi"),
    ],
)
def test_format_distance_is_imperial_and_never_zero_miles(meters, expected):
    assert format_distance(meters) == expected
