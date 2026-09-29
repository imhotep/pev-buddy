# PEV Buddy

**Find EV charging in San Francisco and ride there the sensible way — turn-by-turn
e-bike/e-scooter/moped routing that weighs every street by its speed, built
entirely on [Overture Maps](https://overturemaps.org) data.**

## Why this

San Francisco's EV charging map is a scatter of stations, but the real question
an e-bike or e-scooter rider asks is *"how do I actually get there without ending
up in 45 mph traffic?"* PEV Buddy answers both in one product: it lists SF's
charging stations **and** computes a route to each one. Instead of a hard
"≤ 25 mph only" filter, the router is **speed-differential** and
**vehicle-aware**: you pick your CA vehicle type (e-scooter, e-skateboard,
EUC, Class 1/2/3 e-bike, moped — e-motos are shown but blocked, since they're
not street-legal), and every street is categorized by its effective speed
limit (posted limit, or a per-class default when Overture doesn't post one)
and checked against that vehicle's road-access rule (e-scooters/EUCs:
≤ 25 mph or in bike lanes; e-boards: under 35 mph; e-bikes/mopeds: no
road-speed restriction) and priced by its cost profile — bike lanes and quiet
streets are cheap, shared streets cost more, arterials cost more still, and
anything above 45 mph is excluded outright. One-way streets, prohibited
turns, and Overture access rules (bicycle mode for most types, motorcycle
mode for mopeds) are respected, not just ignored.

Everything — stations, the routing network, and address search — comes from the
Overture open dataset (no OSM scraping, no Google, no commercial API). Bike
*parking* comes from [BikeLink](https://bikelink.org) — secure smart lockers
that keep a PEV or bike safe — served as a second, differently-colored layer.

## How it works

1. **Extraction** (`python -m pev_buddy.sync`) pulls five Overture slices for SF
   from their S3 bucket, plus the SF BikeLink locker slice, and writes compact
   static files into `data/`:

   | file | source | notes |
   |---|---|---|
   | `roads.geojson` | Overture `transportation/segment` | roads only, trimmed to routing-relevant fields |
   | `connectors.json` | Overture `transportation/connector` | junction nodes |
   | `stations.geojson` | Overture `place` (`basic_category: ev_charging_station`) | 40 SF chargers |
   | `places.json` | Overture `place` (all other named places) | ~81k businesses/POIs for place search |
   | `addresses.parquet` | Overture `addresses/address` | ~433k addresses for search |
   | `bikelink.geojson` | [bikelink.org](https://bikelink.org) `/maps` payload | SF secure bike/PEV lockers (eLockers, hangars, group parking), ~29 sites |

2. **Routing** (`src/pev_buddy/`) builds one directed *union* graph from
   segments + connectors (every edge any vehicle type may ride) and runs A*
   over `(node, incoming-edge)` states so U-turns and Overture's
   `prohibited_transitions` are handled correctly. At build time each
   vehicle type gets a 1-byte allow bitmap per edge (and per node, for
   snapping), so per-vehicle routing is a bitmap check plus a cost lookup.

   **CA vehicle types.** All vehicle types are defined as data in
   `src/pev_buddy/config.py` (`VEHICLE_TYPES`); the UI dropdown, the API, and
   the router all read from it. Each type carries its label, description,
   own speed limit, road-access rule (`road_max_mph`, e.g. the e-scooter's
   ≤ 25 mph / boards' under-35 mph), whether bike lanes are exempt from that
   limit, which Overture access mode applies (bicycle; motorcycle for
   mopeds), and a cost profile:

   | id | label | road access | profile |
   |---|---|---|---|
   | `scooter` (default) | E-scooter | roads ≤ 25 mph or in bike lanes | A |
   | `emb` | E-skateboard / Onewheel | roads under 35 mph | A |
   | `euc` | Electric unicycle | same as `scooter` (conservative) | A |
   | `ebike_c1` / `ebike_c2` | Class 1/2 e-bike | no road-speed restriction | A |
   | `ebike_c3` | Class 3 e-bike | no road-speed restriction | B |
   | `moped` | Moped | motor-vehicle rules | C |
   | `emoto` | E-moto | not street-legal — shown in the UI, but route requests are blocked | — |

   **Speed-differential cost model.** Each segment heading gets a category —
   `bike_lane` (cycleway/path/track or a designated bicycle facility),
   `living_street`, `sidewalk` (footway: a high-cost walk-your-vehicle
   connector), or a speed band from its effective limit (posted limit wins
   over the per-class default, e.g. residential 25 / tertiary 30 / secondary
   40 / motorway 65): `quiet` ≤ 20 mph, `shared` 20–28, `arterial` 28–45.
   Cost is `length_m × cost_profile[category]` (e.g. arterial = 3.0× for
   profile A, 1.6× for B, 1.0× for C — dedicated bike lanes are 3.0× for
   mopeds, which are usually bicycle-only). Effective limit > 45 mph is
   excluded for every vehicle type, as are stairs/bridleways; Overture
   `access_restrictions` clear the corresponding heading bits per access
   mode. The API reports a per-category `segment_stats` breakdown with
   warnings for sidewalk sections, fast arterials, shared roadways, and —
   for mopeds — dedicated bike lanes.

3. **API** (FastAPI) serves the static files plus `/api/route` (takes a
   `vehicle` type; blocks non-street-legal ones), `/api/vehicles` (the
   vehicle types + default, straight from config), `/api/search` (unified
   addresses + places/POIs + charging stations, ranked), `/api/geocode`, and
   `/api/stations`.

4. **UI** is a dependency-free vanilla-JS + MapLibre GL single page: dark map
   with the rideable network drawn, chargers as clickable markers, a vehicle
   type dropdown (options + description loaded from `/api/vehicles`) with a
   live description line, a unified search that accepts any address,
   business, or POI for either start or destination (plus click-anywhere and
   GPS), and a turn-by-turn panel where each step can be highlighted on the
   map.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pev_buddy.sync        # extract the SF Overture slice (~50 MB)
uvicorn pev_buddy.api:app --reload
# open http://localhost:8000
```

Tests: `pytest` (75 tests, all offline/synthetic — no S3 calls).

## Key trade-offs & cuts (v1)

- **Static city slice.** Data is extracted once and served from disk (no live
  Overture queries), which keeps the app fast and free-tier friendly. Stale
  between syncs; re-run `sync` to refresh.
- **Routing is *advisory*, not live.** No traffic, no closed lanes, no
  bike-signal awareness. Costs are distance × per-(vehicle-type, category)
  preference (bike lanes and quiet streets preferred), not travel-time
  estimation. Road-access rules are data in `config.VEHICLE_TYPES`, so
  per-city overrides can be layered on without touching the router.
- **Chargers are station-level only.** Overture places carry names/brands/
  addresses but not plug counts, kW, or live availability — the UI honestly
  says what it knows and links out for the rest.
- **Speeds are inferred when unposted.** Overture doesn't post a speed on
  every street, so unposted streets fall back to per-class defaults
  (`SPEED_BY_CLASS_DEFAULT_MPH`); a posted limit always wins. Mode-scoped
  speed limits (e.g. hgv-only) are ignored, and nothing above 45 mph is ever
  routed. The result is a *soft* speed policy — fast streets are expensive,
  not forbidden — so a walled-in destination is still reachable via an
  arterial (with a warning) instead of a dead "no route" error.
- **Turns are geometry-derived.** Instructions come from bearings between
  segment endpoints plus Overture signpost `destinations` where available;
  there's no per-intersection camera-style guidance.

## Data & licenses

Map data: [Overture Maps](https://overturemaps.org), released under
[CDLA-Permissive-2.0](https://opendatacommons.org/licenses/pddl/2.0/), with
OSM-derived upstream attribution. Basemap tiles: CARTO. BikeLink locker
locations: [bikelink.org](https://bikelink.org) (© eLOCK Technologies LLC),
pulled from the public locations page at sync time — the slice is refresh
stable and small, and a failed pull never breaks the build.
