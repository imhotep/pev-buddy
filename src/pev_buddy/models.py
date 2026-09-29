"""Pydantic models for the API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from . import config


class PointRef(BaseModel):
    """A start/end reference: coords, a geocode query, or a station id."""

    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    query: str | None = None
    station_id: str | None = None
    label: str | None = None  # display name for coordinate refs (e.g. a searched POI)


class RouteRequest(BaseModel):
    start: PointRef
    end: PointRef
    vehicle: str = config.DEFAULT_VEHICLE  # CA vehicle type id, e.g. "scooter" | "ebike_c3" | "moped"


class StationOut(BaseModel):
    id: str | None
    name: str | None
    brand: str | None
    address: str | None
    phone: str | None
    website: str | None
    lon: float
    lat: float
    distance_m: float | None = None


class GeocodeOut(BaseModel):
    text: str
    street: str
    number: str | None
    unit: str | None
    postcode: str | None
    lon: float
    lat: float


class BikeLinkOut(BaseModel):
    id: str | None
    name: str | None
    facility_type: str | None
    address: str | None
    lon: float
    lat: float
    num_spaces: int | None = None
    distance_m: float | None = None


class SearchItem(BaseModel):
    """A unified start/destination candidate: address, POI, station, or BikeLink locker."""

    kind: str  # "address" | "place" | "station" | "bikelink"
    text: str
    category: str | None = None
    lon: float
    lat: float
    address: str | None = None
    station_id: str | None = None


class RouteStepOut(BaseModel):
    index: int
    maneuver: str
    instruction: str
    road: str | None
    distance_m: float
    duration_s: float
    turn_point: list[float] | None
    bearing_after: float | None
    geometry: list[list[float]] | None = None


class SegmentStat(BaseModel):
    """Per-category breakdown of a route (distance + top posted limit)."""

    distance_m: float
    max_speed_mph: int | None = None


class RouteOut(BaseModel):
    path: list[list[float]]
    distance_m: float
    duration_s: float
    steps: list[RouteStepOut]
    warnings: list[str]
    start_label: str | None = None
    end_label: str | None = None
    vehicle: str | None = None
    segment_stats: dict[str, SegmentStat] | None = None
