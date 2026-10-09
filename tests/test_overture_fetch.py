"""Access-rule handling in trim_segment (Overture segment -> routing masks)."""

from pev_buddy.overture_fetch import trim_segment


def _when(**kw):
    base = {"during": None, "heading": None, "using": None, "recognized": None, "mode": None, "vehicle": None}
    return {**base, **kw}


def _segment(rules):
    return {
        "class": "residential",
        "access_restrictions": rules,
        "connectors": [{"connector_id": "a", "at": 0.0}, {"connector_id": "b", "at": 1.0}],
    }


# Overture's rules for SF's Cayuga Avenue: a designated bike route with a
# 3 t weight limit for motor traffic.
CAYUGA_RULES = [
    {"access_type": "designated", "when": _when(mode=["bicycle"]), "between": None},
    {"access_type": "allowed", "when": _when(using=["at_destination"], mode=["motor_vehicle"]), "between": None},
    {
        "access_type": "denied",
        "when": _when(vehicle=[{"dimension": "weight", "comparison": "greater_than", "value": 3.0, "unit": "t"}]),
        "between": None,
    },
]


def test_weight_limit_does_not_close_street_to_bicycles():
    t = trim_segment(_segment(CAYUGA_RULES))
    assert t["ow"] == 3 and t["ow_moto"] == 3
    assert t["bike_desig"] == 3


def test_unconditional_denial_still_closes_street():
    t = trim_segment(_segment([{"access_type": "denied", "when": _when(), "between": None}]))
    assert t["ow"] == 0 and t["ow_moto"] == 0


def test_one_way_denial_still_applies():
    t = trim_segment(_segment([{"access_type": "denied", "when": _when(heading="backward"), "between": None}]))
    assert t["ow"] == 1 and t["ow_moto"] == 1


def test_mode_scoped_denial_applies_only_to_that_mode():
    t = trim_segment(_segment([{"access_type": "denied", "when": _when(mode=["bicycle"]), "between": None}]))
    assert t["ow"] == 0 and t["ow_moto"] == 3
