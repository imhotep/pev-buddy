"""FastAPI application: routing API + static web app."""

from __future__ import annotations

import gc
import time
from contextlib import asynccontextmanager
from pathlib import Path

import orjson
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.gzip import GZipMiddleware

from . import config
from .bikelink import BikeLinkStore
from .geo import format_distance
from .geocode import REVERSE_EXACT_M, Geocoder, display_address
from .graph import RoadGraph
from .models import (
    BikeLinkOut,
    GeocodeOut,
    PointRef,
    ReverseOut,
    RouteOut,
    RouteRequest,
    RouteStepOut,
    SearchItem,
    StationOut,
)
from .pois import POIIndex
from .routing import RouteError, find_route
from .stations import StationStore
from .turns import build_steps


class AppState:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.graph: RoadGraph | None = None  # union graph, per-vehicle masks inside
        self.stations: StationStore | None = None
        self.bikelink: BikeLinkStore | None = None
        self.geocoder: Geocoder | None = None
        self.pois: POIIndex | None = None
        self.build_started_s = 0.0

    def warm(self) -> None:
        t0 = time.time()
        # Prefer the compact runtime bundle (written by sync); fall back to
        # building from the raw Overture slices (dev/tests).
        if (self.data_dir / "graph.npz").exists() and (self.data_dir / "graph_meta.json").exists():
            self.graph = RoadGraph.load_bundle(self.data_dir)
        else:
            roads = orjson.loads((self.data_dir / "roads.geojson").read_bytes())
            connectors = orjson.loads((self.data_dir / "connectors.json").read_bytes())
            self.graph = RoadGraph()
            self.graph.build(roads, connectors)
            del roads, connectors
            gc.collect()
        self.stations = StationStore(self.data_dir / "stations.geojson")
        if (self.data_dir / "bikelink.geojson").exists():
            self.bikelink = BikeLinkStore(self.data_dir / "bikelink.geojson")
        addresses_npz = self.data_dir / "addresses.npz"
        self.geocoder = Geocoder(
            addresses_npz if addresses_npz.exists() else self.data_dir / "addresses.parquet"
        )
        if (self.data_dir / "places.json").exists():
            self.pois = POIIndex(self.data_dir / "places.json")
        self.build_started_s = time.time() - t0


def create_app(data_dir: str | None = None) -> FastAPI:
    data_dir = Path(data_dir) if data_dir else config.DATA_DIR

    state = AppState(data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if (data_dir / "roads.geojson").exists():
            state.warm()
        yield

    app = FastAPI(title="PEV Buddy", lifespan=lifespan)
    # roads.geojson is ~39 MB raw — gzip makes the initial map load ~5x lighter
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    def resolve_point(ref: PointRef, kind: str) -> tuple[float, float, str]:
        if ref.station_id:
            st = next((s for s in state.stations.all() if s["id"] == ref.station_id), None)
            if st is None and state.bikelink is not None:
                st = next((s for s in state.bikelink.all() if s["id"] == ref.station_id), None)
                if st is not None:
                    return st["lon"], st["lat"], st["name"] or "BikeLink locker"
            if st is None:
                raise HTTPException(404, f"unknown station {ref.station_id}")
            return st["lon"], st["lat"], st["name"] or "charging station"
        if ref.query:
            results = state.geocoder.search(ref.query, limit=1)
            if not results:
                raise HTTPException(400, f"could not find address {ref.query!r}")
            r = results[0]
            return r["lon"], r["lat"], r["text"]
        if ref.lat is not None and ref.lon is not None:
            return ref.lon, ref.lat, ref.label or "map location"
        raise HTTPException(400, f"{kind} must specify lat/lon, query, or station_id")

    @app.get("/api/health")
    def health() -> dict:
        manifest = {}
        mp = data_dir / "manifest.json"
        if mp.exists():
            manifest = orjson.loads(mp.read_bytes())
        g = state.graph
        return {
            "status": "ok",
            "release": manifest.get("release"),
            "nodes": g.node_count if g else 0,
            "edges": g.edge_count if g else 0,
            "stations": len(state.stations) if state.stations else 0,
            "bikelink": len(state.bikelink) if state.bikelink else 0,
            "build_seconds": round(state.build_started_s, 2),
            "vehicles": g.edge_counts if g else {},
            "default_vehicle": config.DEFAULT_VEHICLE,
        }

    @app.get("/api/vehicles")
    def vehicle_types() -> dict:
        """The CA vehicle types, straight from config — the UI's single source."""
        return {
            "default": config.DEFAULT_VEHICLE,
            "vehicles": [
                {
                    "id": config.VEHICLE_TYPES[vid].id,
                    "label": config.VEHICLE_TYPES[vid].label,
                    "description": config.VEHICLE_TYPES[vid].description,
                    "routable": config.VEHICLE_TYPES[vid].routable,
                    "speed_limit_mph": config.VEHICLE_TYPES[vid].speed_limit_mph,
                }
                for vid in config.VEHICLE_ORDER
            ],
        }

    @app.get("/api/stations", response_model=list[StationOut])
    def stations() -> list[dict]:
        if state.stations is None:
            raise HTTPException(503, "data not loaded yet")
        return sorted(state.stations.all(), key=lambda s: (s["name"] or "").lower())

    @app.get("/api/stations/nearest", response_model=list[StationOut])
    def nearest(lat: float, lon: float, limit: int = 5) -> list[dict]:
        if state.stations is None:
            raise HTTPException(503, "data not loaded yet")
        return state.stations.nearest(lon, lat, limit=min(max(limit, 1), 25))

    @app.get("/api/bikelink", response_model=list[BikeLinkOut])
    def bikelink() -> list[dict]:
        if state.bikelink is None:
            raise HTTPException(503, "data not loaded yet")
        return sorted(state.bikelink.all(), key=lambda s: (s["name"] or "").lower())

    @app.get("/api/bikelink/nearest", response_model=list[BikeLinkOut])
    def bikelink_nearest(lat: float, lon: float, limit: int = 5) -> list[dict]:
        if state.bikelink is None:
            raise HTTPException(503, "data not loaded yet")
        return state.bikelink.nearest(lon, lat, limit=min(max(limit, 1), 25))

    @app.get("/api/geocode", response_model=list[GeocodeOut])
    def geocode(q: str, limit: int = 10) -> list[dict]:
        if state.geocoder is None:
            raise HTTPException(503, "data not loaded yet")
        return state.geocoder.search(q, limit=min(max(limit, 1), 25))

    @app.get("/api/reverse", response_model=ReverseOut)
    def reverse(lat: float, lon: float) -> dict:
        """Label a map-picked point by its nearest address ("Near 123 Valencia
        St"), falling back to "Dropped pin" when no address is within range."""
        if state.geocoder is None:
            raise HTTPException(503, "data not loaded yet")
        hit = state.geocoder.reverse(lon, lat)
        if hit is None:
            return {"label": "Dropped pin", "address": None, "distance_m": None, "lon": lon, "lat": lat}
        address = display_address(hit["number"], hit["street"])
        exact = hit["distance_m"] <= REVERSE_EXACT_M
        return {
            "label": address if exact else f"Near {address}",
            "address": address,
            "distance_m": round(hit["distance_m"], 1),
            "lon": lon,
            "lat": lat,
        }

    @app.get("/api/search", response_model=list[SearchItem])
    def search(q: str, limit: int = 8) -> list[dict]:
        """Unified start/destination search: addresses, places/POIs, stations, BikeLink lockers."""
        if state.geocoder is None and state.pois is None and state.stations is None and state.bikelink is None:
            raise HTTPException(503, "data not loaded yet")
        limit = min(max(limit, 1), 15)
        ql = q.strip()
        if not ql:
            return []
        ql_l = ql.lower()

        items: list[dict] = []
        if state.stations is not None:
            for s in state.stations.all():
                nm = (s.get("name") or "").lower()
                if not nm:
                    continue
                score = 100 if nm == ql_l else 80 if nm.startswith(ql_l) else 60 if ql_l in nm else 0
                if score:
                    items.append(
                        {
                            "kind": "station",
                            "text": s["name"],
                            "category": "EV Charging",
                            "lon": s["lon"],
                            "lat": s["lat"],
                            "address": s.get("address"),
                            "station_id": s.get("id"),
                            "score": score,
                        }
                    )
        if state.bikelink is not None:
            for s in state.bikelink.all():
                nm = (s.get("name") or "").lower()
                if not nm:
                    continue
                score = 100 if nm == ql_l else 80 if nm.startswith(ql_l) else 60 if ql_l in nm else 0
                if score:
                    items.append(
                        {
                            "kind": "bikelink",
                            "text": s["name"],
                            "category": "Bike Parking",
                            "lon": s["lon"],
                            "lat": s["lat"],
                            "address": s.get("address"),
                            "station_id": s.get("id"),
                            "score": score,
                        }
                    )
        if state.pois is not None:
            for r in state.pois.search(ql, limit=limit * 2):
                items.append(
                    {
                        "kind": "place",
                        "text": r["name"],
                        "category": r["category"],
                        "lon": r["lon"],
                        "lat": r["lat"],
                        "address": r["address"],
                        "station_id": None,
                        "score": r["score"],
                    }
                )
        if state.geocoder is not None:
            for r in state.geocoder.search(ql, limit=limit):
                items.append(
                    {
                        "kind": "address",
                        "text": r["text"],
                        "category": "Address",
                        "lon": r["lon"],
                        "lat": r["lat"],
                        "address": None,
                        "station_id": None,
                        "score": r["score"],
                    }
                )

        items.sort(key=lambda x: (-x["score"], x["text"].casefold()))
        out: list[dict] = []
        seen: set = set()
        for it in items:
            key = (it["kind"], it["text"].casefold())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "kind": it["kind"],
                    "text": it["text"],
                    "category": it["category"],
                    "lon": it["lon"],
                    "lat": it["lat"],
                    "address": it["address"],
                    "station_id": it["station_id"],
                }
            )
            if len(out) >= limit:
                break
        return out

    @app.post("/api/route", response_model=RouteOut)
    def route(req: RouteRequest) -> dict:
        if state.graph is None:
            raise HTTPException(503, "network not loaded yet")
        vehicle = req.vehicle or config.DEFAULT_VEHICLE
        veh = config.VEHICLE_TYPES.get(vehicle)
        if veh is None:
            raise HTTPException(
                400, f"unknown vehicle type {req.vehicle!r}; expected one of: {', '.join(config.VEHICLE_ORDER)}"
            )
        if not veh.routable:
            raise HTTPException(
                400,
                f"{veh.label} is not street-legal in California — off-highway use only. "
                "Pick a street-legal vehicle type.",
            )
        graph = state.graph
        try:
            slon, slat, slabel = resolve_point(req.start, "start")
            elon, elat, elabel = resolve_point(req.end, "end")
            result = find_route(graph, slon, slat, elon, elat, vehicle=vehicle)
        except HTTPException:
            raise
        except RouteError as e:
            raise HTTPException(400, str(e))
        warnings = []
        if result.start_snap_m > 50:
            warnings.append(f"start snapped {format_distance(result.start_snap_m)} to the nearest junction")
        if result.end_snap_m > 50:
            warnings.append(f"destination snapped {format_distance(result.end_snap_m)} to the nearest junction")
        stats = result.segment_stats
        sidewalk = stats.get("sidewalk")
        if sidewalk and sidewalk["distance_m"]:
            warnings.append(
                f"route includes {format_distance(sidewalk['distance_m'])} of sidewalk — walk your vehicle where riding is prohibited"
            )
        arterial = stats.get("arterial")
        if arterial and arterial["distance_m"]:
            mph = f" (up to {arterial['max_speed_mph']} mph posted)" if arterial["max_speed_mph"] else ""
            warnings.append(
                f"route uses {format_distance(arterial['distance_m'])} of fast arterial street{mph} — share the road with care"
            )
        if vehicle == "moped":
            bike_lane = stats.get("bike_lane")
            if bike_lane and bike_lane["distance_m"]:
                warnings.append(
                    "route uses a dedicated bike lane that is usually reserved for bicycles"
                )
        shared_m = sum(
            stats.get(c, {}).get("distance_m", 0.0) for c in ("shared", "arterial")
        )
        if result.distance_m and shared_m > max(200.0, 0.3 * result.distance_m):
            warnings.append(
                f"route shares the roadway with car traffic for {format_distance(shared_m)} — ride alert"
            )
        steps = build_steps(graph, result.edge_idxs, elabel)
        return {
            "path": result.path,
            "distance_m": round(result.distance_m, 1),
            "duration_s": round(result.duration_s),
            "steps": [
                {
                    "index": s["index"],
                    "maneuver": s["maneuver"],
                    "instruction": s["instruction"],
                    "road": s["road"],
                    "distance_m": round(s["distance_m"], 1),
                    "duration_s": round(s["duration_s"]),
                    "turn_point": s["turn_point"],
                    "bearing_after": None if s["bearing_after"] is None else round(s["bearing_after"], 1),
                    "geometry": s["geometry"],
                }
                for s in steps
            ],
            "warnings": warnings,
            "start_label": slabel,
            "end_label": elabel,
            "vehicle": vehicle,
            "segment_stats": {
                c: {"distance_m": round(v["distance_m"], 1), "max_speed_mph": v["max_speed_mph"]}
                for c, v in stats.items()
            },
        }

    def geojson_file(name: str) -> FileResponse:
        p = data_dir / name
        if not p.exists():
            raise HTTPException(404, f"{name} not available")
        # streamed from disk — read_bytes() would spike tens of MB per request
        return FileResponse(p, media_type="application/geo+json")

    @app.get("/data/roads.geojson", include_in_schema=False)
    def roads_geojson() -> Response:
        return geojson_file("roads.geojson")

    @app.get("/data/stations.geojson", include_in_schema=False)
    def stations_geojson() -> Response:
        return geojson_file("stations.geojson")

    @app.get("/data/bikelink.geojson", include_in_schema=False)
    def bikelink_geojson() -> Response:
        return geojson_file("bikelink.geojson")

    @app.get("/data/racks.geojson", include_in_schema=False)
    def racks_geojson() -> Response:
        return geojson_file("racks.geojson")

    web_dir = config.PROJECT_ROOT / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


app = create_app()
