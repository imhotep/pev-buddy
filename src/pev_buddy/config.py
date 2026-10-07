"""Configuration: San Francisco scope, Overture release, and routing policy."""

import os
from dataclasses import dataclass
from pathlib import Path


def _project_root() -> Path:
    """Where data/ and web/ live.

    A regular (non-editable) pip install puts this file in site-packages, so
    the package-derived root only works for editable installs. When it lacks
    web/, fall back to the working directory — uvicorn and `sync` are both
    run from the repo checkout (locally and on Render).
    """
    pkg_root = Path(__file__).resolve().parent.parent.parent
    if (pkg_root / "web").is_dir():
        return pkg_root
    cwd = Path.cwd()
    return cwd if (cwd / "pyproject.toml").exists() else pkg_root


PROJECT_ROOT = _project_root()
DATA_DIR = Path(os.environ.get("PEV_BUDDY_DATA", PROJECT_ROOT / "data"))

# San Francisco plus a little headroom (bridge approaches, Muni reach).
SF_BBOX = (-122.55, 37.65, -122.30, 37.87)

OVERTURE_RELEASE = os.environ.get("PEV_BUDDY_RELEASE", "2026-09-23.0")

# --- Routing policy (CA vehicle types) -------------------------------------

# Overture travel mode this app routes for (e-bike / e-scooter).
TRAVEL_MODE = "bicycle"


# A street-legal (or not) vehicle type in California. The single source of
# truth for what the rider is on: the UI dropdown, the API, and the router
# all read from VEHICLE_TYPES. Road-access rules live here as data so
# per-city overrides can be layered on later without touching the router.
@dataclass(frozen=True)
class VehicleType:
    id: str
    label: str
    description: str
    speed_limit_mph: float | None  # the vehicle's own CA speed cap (informational)
    routable: bool  # False = shown in the UI but never routed for
    profile: str | None  # cost profile key into COST_PROFILES
    road_max_mph: float | None  # max effective road speed usable; None = no
    # road-speed restriction (still bounded by the universal MAX_SPEED_MPH)
    road_max_inclusive: bool = True  # is road_max_mph itself allowed?
    bike_lane_exempt: bool = False  # dedicated bike facilities are usable even
    # on roads above road_max_mph (Class II/IV lanes)
    access_mode: str = "bicycle"  # which Overture access-restriction mode
    # applies to this vehicle ("bicycle" or "motorcycle")


VEHICLE_TYPES: dict[str, VehicleType] = {
    "scooter": VehicleType(
        "scooter",
        "E-scooter",
        "Stand-up e-scooters with handlebars. CA limit 15 mph; roads ≤ 25 mph or in bike lanes.",
        15,
        True,
        "A",
        25,
        True,
        True,
    ),
    "emb": VehicleType(
        "emb",
        "E-skateboard / Onewheel",
        "Electric boards, including Onewheels. CA limit 15 mph; roads under 35 mph.",
        15,
        True,
        "A",
        35,
        False,
        True,
    ),
    "euc": VehicleType(
        "euc",
        "Electric unicycle (EUC)",
        "Legal gray area: EUCs have no specific CA vehicle class. Routed conservatively. Check local rules.",
        15,
        True,
        "A",
        25,
        True,
        True,
    ),
    "ebike_c1": VehicleType(
        "ebike_c1", "Class 1 e-bike", "Pedal assist only, no throttle. Up to 20 mph.", 20, True, "A", None
    ),
    "ebike_c2": VehicleType(
        "ebike_c2", "Class 2 e-bike", "Throttle allowed. Up to 20 mph.", 20, True, "A", None
    ),
    "ebike_c3": VehicleType(
        "ebike_c3", "Class 3 e-bike", "Pedal assist only, up to 28 mph. Riders 16+.", 28, True, "B", None
    ),
    "moped": VehicleType(
        "moped",
        "Moped",
        "Up to 30 mph. Requires a license and plate.",
        30,
        True,
        "C",
        None,
        access_mode="motorcycle",
    ),
    "emoto": VehicleType(
        "emoto",
        "E-moto (Sur-Ron, Talaria, etc.)",
        "Not street legal in California. Off-highway use only.",
        None,
        False,
        None,
        None,
    ),
}

# Dropdown order / canonical ordering.
VEHICLE_ORDER = ("scooter", "emb", "euc", "ebike_c1", "ebike_c2", "ebike_c3", "moped", "emoto")

# Most restrictive street-legal type: the default selection.
DEFAULT_VEHICLE = "scooter"


def vehicle_allows(veh: VehicleType, category: str, effective_speed_mph: float) -> bool:
    """Can `veh` ride a road segment of `category` at this effective speed?

    Pure data lookup — no city- or segment-specific conditionals, so a
    per-city override just replaces fields on the VehicleType.
    """
    if veh.road_max_mph is None:
        return True  # no road-speed restriction (e-bikes, mopeds)
    if category == "bike_lane" and veh.bike_lane_exempt:
        return True  # Class II/IV bike lanes: usable above the road limit
    if category in ("sidewalk", "living_street"):
        return True  # no roadway-speed concept applies
    if veh.road_max_inclusive:
        return effective_speed_mph <= veh.road_max_mph
    return effective_speed_mph < veh.road_max_mph

# Overture does not post a speed on every street; fall back to these
# per-class values (mph) when no limit is posted.
SPEED_BY_CLASS_DEFAULT_MPH = {
    "residential": 25,
    "living_street": 15,
    "unclassified": 25,
    "service": 20,
    "pedestrian": 10,
    "tertiary": 30,
    "secondary": 40,
    "trunk": 50,
    "primary": 55,
    "motorway": 65,
}
DEFAULT_SPEED_MPH = 25  # unknown road class

# Classes no vehicle type ever routes on (stairs / bridle paths: impassable).
# High-speed classes are excluded through MAX_SPEED_MPH instead.
EXCLUDED_CLASSES = frozenset({"steps", "bridleway"})

# Above this effective speed (mph) a segment is excluded for every vehicle
# type (freeway-grade roads are never ridden, whatever the vehicle).
MAX_SPEED_MPH = 45

# Speed bands for the cost categories (mph):
#   quiet:    effective limit <= QUIET_MAX_MPH
#   shared:   QUIET_MAX_MPH < limit <= SHARED_MAX_MPH
#   arterial: SHARED_MAX_MPH < limit <= MAX_SPEED_MPH
QUIET_MAX_MPH = 20.0
SHARED_MAX_MPH = 28.0

# Cost profiles: multipliers per (profile, category); edge cost =
# length_m * multiplier. Vehicle types map onto profiles:
#   A — e-scooter, e-skateboard, EUC, Class 1/2 e-bikes (conservative)
#   B — Class 3 e-bike (comfortable on shared streets and arterials)
#   C — moped (street-legal at speed; dedicated bike lanes discouraged)
# Categories:
#   bike_lane     cycleway/path/track, or a segment with a designated
#                 bicycle access rule (dedicated or painted bike facility)
#   living_street traffic-calmed street
#   sidewalk      footway — ride only as a last resort (walk-your-vehicle
#                 connector, e.g. to reach a point walled in by fast streets)
#   quiet / shared / arterial: speed bands above
COST_PROFILES = {
    "A": {"bike_lane": 0.85, "living_street": 0.9, "sidewalk": 20.0, "quiet": 1.0, "shared": 1.3, "arterial": 3.0},
    "B": {"bike_lane": 0.85, "living_street": 0.9, "sidewalk": 20.0, "quiet": 1.0, "shared": 1.0, "arterial": 1.6},
    "C": {"bike_lane": 3.0, "living_street": 0.9, "sidewalk": 20.0, "quiet": 1.1, "shared": 0.9, "arterial": 1.0},
}
CATEGORIES = ("bike_lane", "living_street", "sidewalk", "quiet", "shared", "arterial")

# --- ETA model ---------------------------------------------------------------
# Per-edge travel speed = min(vehicle speed cap, road effective limit)
#                         * ETA_REALISM_FACTOR
# The factor turns a legal cap into an urban *average*: SF's short blocks mean
# frequent stop signs, signals, and yielding, so riders spend a large share of
# each block accelerating/braking below the cap. 0.75 is the conservative end
# of the 0.75-0.85 band (an ETA that is slightly long is better than one that
# is optimistic for a battery-limited vehicle); hills are not modeled yet.
ETA_REALISM_FACTOR = 0.75

# Sidewalk connectors are walk-your-vehicle segments: walking pace.
SIDEWALK_WALK_MPH = 3.0

# Fallback cap for a routable vehicle with no speed_limit_mph (none today).
AVG_SPEED_MPH = 15.0

MPH_TO_MPS = 0.44704


def travel_speed_mph(veh: VehicleType, category: str, road_mph: float | None) -> float:
    """Expected average speed (mph) for `veh` on one edge.

    Sidewalks are walked; everything else rides at
    min(vehicle cap, road effective limit) scaled by ETA_REALISM_FACTOR.
    """
    if category == "sidewalk":
        return SIDEWALK_WALK_MPH
    cap = veh.speed_limit_mph or AVG_SPEED_MPH
    if road_mph is not None and road_mph > 0:
        cap = min(cap, road_mph)
    return cap * ETA_REALISM_FACTOR


def edge_duration_s(veh: VehicleType, length_m: float, category: str, road_mph: float | None) -> float:
    """Seconds to traverse `length_m` of an edge (see travel_speed_mph)."""
    mph = travel_speed_mph(veh, category, road_mph)
    return length_m / (mph * MPH_TO_MPS) if mph > 0 else 0.0


# How far (meters) a start/end point may be from the network before we give up.
SNAP_RADIUS_M = 200.0

# A* / Dijkstra tuning.
REVERSAL_PENALTY = 4.0  # extra cost factor for an immediate U-turn
