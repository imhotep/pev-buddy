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
# Where routes may start and end: the city itself (the extract above is
# wider, so places across the Bay show up in the data). The frontend's
# SF_BOUNDS is the same box.
SERVICE_BOUNDS = (-122.52, 37.70, -122.35, 37.84)


# --- HTTP security headers (api.py adds them to every response) --------------
# The page loads only its own files (MapLibre is vendored under web/vendor/)
# plus the CARTO basemap (style, tiles, fonts, sprites); MapLibre decodes
# tiles in blob: workers. Inline styles stay allowed: MapLibre and the debug
# panel set them. Geolocation, motion sensors (the compass) and the wake lock
# are the app's own; nothing else.
_CARTO = "https://basemaps.cartocdn.com https://*.basemaps.cartocdn.com"
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        f"img-src 'self' data: blob: {_CARTO}",
        f"connect-src 'self' {_CARTO}",
        "worker-src 'self' blob:",
        "font-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "report-uri /api/csp-report",
    ]
)
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": (
        "geolocation=(self), accelerometer=(self), gyroscope=(self), magnetometer=(self), "
        "screen-wake-lock=(self), camera=(), microphone=(), payment=(), usb=()"
    ),
}


def in_service_area(lon: float, lat: float) -> bool:
    xmin, ymin, xmax, ymax = SERVICE_BOUNDS
    return xmin <= lon <= xmax and ymin <= lat <= ymax

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
    avoid_traffic: bool = False  # slow, small-wheeled: keep off streets with
    # cars wherever a bike path or lane goes, and show where it can't


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
        "Electric boards, including Onewheels. CA limit 15 mph. Keeps to bike paths and lanes wherever "
        "they go; streets with cars (25 mph or less) only where there's no other way, shown in red.",
        15,
        True,
        "D",
        25,
        True,
        True,
        avoid_traffic=True,
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
    if category == "steps":
        return False  # stairs: walking only
    if veh.road_max_mph is None:
        return True  # no road-speed restriction (e-bikes, mopeds)
    if category in ("bike_lane", "bike_path") and veh.bike_lane_exempt:
        return True  # Class I/II/IV bikeways: usable above the road limit
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

# Classes no one routes on (bridle paths). Stairs are in the graph for the
# walk from a parking spot to the destination (category "steps"), but no
# vehicle type may ride them. High-speed classes are excluded through
# MAX_SPEED_MPH instead.
EXCLUDED_CLASSES = frozenset({"bridleway"})

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
#   A — e-scooter, EUC, Class 1/2 e-bikes (conservative)
#   B — Class 3 e-bike (comfortable on shared streets and arterials)
#   C — moped (street-legal at speed; dedicated bike lanes discouraged)
#   D — e-skateboard / Onewheel: slow and small-wheeled, so riding in
#       traffic is the danger. Bike paths are cheapest, painted lanes next;
#       every street with cars costs several times its length, so a route
#       goes well out of its way to stay on bikeways (VehicleType.avoid_traffic).
#       EUCs stay on A: some now reach 60 mph.
# Categories:
#   bike_path     off the street or separated from cars: Overture
#                 cycleway/path/track, SFMTA Class I paths and Class IV
#                 protected bikeways
#   bike_lane     a painted lane beside traffic (SFMTA Class II; without
#                 SFMTA data, Overture's bicycle-designated streets)
#   living_street traffic-calmed street
#   sidewalk      footway — ride only as a last resort (walk-your-vehicle
#                 connector, e.g. to reach a point walled in by fast streets)
#   quiet / shared / arterial: speed bands above
COST_PROFILES = {
    "A": {"bike_lane": 0.85, "living_street": 0.9, "sidewalk": 20.0, "quiet": 1.0, "shared": 1.3, "arterial": 3.0, "steps": 1000.0},
    "B": {"bike_lane": 0.85, "living_street": 0.9, "sidewalk": 20.0, "quiet": 1.0, "shared": 1.0, "arterial": 1.6, "steps": 1000.0},
    "C": {"bike_lane": 3.0, "living_street": 0.9, "sidewalk": 20.0, "quiet": 1.1, "shared": 0.9, "arterial": 1.0, "steps": 1000.0},
    "D": {"bike_lane": 1.4, "living_street": 1.3, "sidewalk": 20.0, "quiet": 3.0, "shared": 5.0, "arterial": 10.0, "steps": 1000.0},
}
# A separated path costs what a lane did for the other profiles, so their
# routes don't change; for D it is the cheapest way to ride.
for _profile, _bike_path in (("A", 0.85), ("B", 0.85), ("C", 3.0), ("D", 0.8)):
    COST_PROFILES[_profile]["bike_path"] = _bike_path
# Index order is stored in graph.npz (edge_cat): append, never reorder.
CATEGORIES = ("bike_lane", "living_street", "sidewalk", "quiet", "shared", "arterial", "steps", "bike_path")
# Where a rider shares the roadway with cars (no bike lane or path).
MIXED_TRAFFIC = frozenset({"quiet", "shared", "arterial"})

# --- Hills -------------------------------------------------------------------
# Edge cost adds, per cost profile, for the climb along an edge (end elevation
# above start, from the junctions' ground elevation):
#   climb_m * climb                        every meter climbed costs `climb`
#                                          meters of flat riding
# + steep_climb_m * climb * steep          the part of the climb above
#                                          STEEP_GRADE costs `steep` times more
# + steep_descent_m * descent              likewise for a descent steeper than
#                                          STEEP_DESCENT_GRADE (braking on small
#                                          wheels)
# Profiles A and D (scooters, EUCs, boards, Class 1/2 e-bikes) have small motors and
# 15-20 mph caps: SF's 15-30% grades stall them or crawl, so hills weigh most.
# Mopeds barely notice. A 100 m block at 20% costs an e-scooter ~1,700 m.
STEEP_GRADE = 0.08
STEEP_DESCENT_GRADE = 0.15
HILL_PROFILES = {
    "A": {"climb": 20.0, "steep": 3.0, "descent": 4.0},
    "B": {"climb": 10.0, "steep": 2.0, "descent": 1.0},
    "C": {"climb": 3.0, "steep": 1.0, "descent": 0.0},
}
HILL_PROFILES["D"] = HILL_PROFILES["A"]  # the same small motors
# ETA: uphill speed = travel speed / (1 + slowdown * grade).
HILL_SLOWDOWN = {"A": 6.0, "B": 4.0, "C": 1.5, "D": 6.0}


def hill_cost(profile: str, length_m, climb_m):
    """Extra cost (meters-equivalent) for riding an edge that rises climb_m
    over length_m (negative climb_m is a descent). Works on numpy arrays."""
    import numpy as np

    hp = HILL_PROFILES[profile]
    length_m = np.asarray(length_m, dtype=np.float64)
    climb_m = np.nan_to_num(np.asarray(climb_m, dtype=np.float64))
    up = np.maximum(climb_m, 0.0)
    down = np.maximum(-climb_m, 0.0)
    steep_up = np.maximum(up - STEEP_GRADE * length_m, 0.0)
    steep_down = np.maximum(down - STEEP_DESCENT_GRADE * length_m, 0.0)
    return up * hp["climb"] + steep_up * hp["climb"] * hp["steep"] + steep_down * hp["climb"] * hp["descent"]

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
# Walking from a parking spot to the destination.
WALK_MPH = 3.0

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
# Snapping skips junctions joined to no more than this many others for the
# vehicle (a stub boxed in by streets it may not ride), trying the nearest
# SNAP_MAX_CANDIDATES within the radius.
SNAP_POCKET_NODES = 30
SNAP_MAX_CANDIDATES = 40
# Trips start and end at the nearest point on a street, not at its nearest
# junction (#51: a 34 m hop on one-way 3rd St became a 550 m loop). A point
# this close to a junction starts or ends there, as before.
SNAP_NODE_M = 3.0
# A sidewalk is drawn a few meters beside its street: within this much
# farther, the street wins (riding a sidewalk is a last resort).
SNAP_SIDEWALK_SLACK_M = 15.0
# Edges considered for a point: those touching its nearest junctions.
SNAP_EDGE_NODES = 64

# A* / Dijkstra tuning.
REVERSAL_PENALTY = 4.0  # extra cost factor for an immediate U-turn
