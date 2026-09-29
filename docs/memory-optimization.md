# PEV Buddy: cutting server memory from ~960 MB to ~316 MB

## Context

PEV Buddy is a FastAPI app that does vehicle-aware routing over San Francisco's
road network (plus geocoding, POI search, charging stations). The whole network
lives in memory: 105k nodes, 277k directed edges, ~68k road segments, 433k
addresses, and 81k named places. Deploying to Render's free tier meant fitting
into a **512 MB RAM** instance — and the app measured **~960 MB** at steady
state. This document explains why, and what we did about it.

## Why the footprint was so high

We profiled component by component (loading each store in turn and measuring
RSS, then confirming with `tracemalloc`):

| Component | Memory | Root cause |
|---|---|---|
| Parsed `roads.geojson` + `connectors.json` | ~400 MB | `orjson.loads` of the 39 MB file produces a Python object tree — a dict per feature, a dict per properties object, a list per coordinate. **The graph kept it alive forever**: each segment stored its full Overture `props` dict (`seg["props"]`) even though it was only used during graph construction, and held references to the parsed coordinate lists. |
| Graph's own structures | ~160 MB | Everything was Python objects: 277k edge 8-tuples (each with fresh int/float objects), 68k segment dicts, per-node adjacency lists. |
| Geocoder | ~133 MB | Two parts: importing **pyarrow** just to read `addresses.parquet` (~31 MB RSS), and **one dict per address** for 433k addresses (~100 MB). |
| POI index | ~70 MB | One dict per place (81.5k) plus a dict-of-lists name index. |
| Interpreter + libraries | ~76 MB | Python, FastAPI, numpy, orjson, uvicorn — the fixed floor. |

Two distinct problems fell out of this:

1. **Steady-state retention** (~960 MB) — the structures above never shrink.
2. **Boot peak** — parsing 39 MB of geojson into Python objects costs ~400 MB
   transient at every startup, which alone nearly exceeds the 512 MB budget.

## What we did

The strategy: **move the expensive parsing to sync time (the build machine),
and have the server load compact, pre-digested artifacts.**

### 1. Runtime bundle for the graph (`graph.npz` + `graph_meta.json`)

- `RoadGraph.build()` no longer stores the `props` dict — build-time fields
  (class, speed, access masks) live in a local list during construction and
  are discarded. This alone releases the ~400 MB parsed tree after build.
- New `save_bundle()` / `load_bundle()` serialize the graph as numpy arrays
  (node coords, edge columns, CSR adjacency, segment geometry offsets, SCC
  data, per-vehicle allow bitmaps) plus a small orjson metadata file (segment
  names/ids, connector ids, prohibited turns, signposts).
- At runtime, edges are an **`EdgeTable`** — columnar arrays behind the same
  tuple-indexing contract the rest of the code expects (~10 MB instead of
  ~70 MB of tuples).
- The A* hot loop reads edge columns through **memoryviews** instead of
  unpacking tuples. Side effect: routing got *faster* (108 ms vs 129 ms for a
  cross-city route).

### 2. Packed geocoder (`addresses.npz` + `addresses_meta.json`)

- Per-address dicts replaced by **vocabulary-coded columns**: house numbers,
  units, and postcodes become int32 codes into small vocab lists; coordinates
  become flat float64 arrays; rows are grouped by street with an offsets
  array. 433k addresses went from ~110 MB to ~15 MB.
- The pyarrow import moved into the parquet code path only, so the runtime
  never imports it (−31 MB). Parquet is still accepted (tests, ad-hoc
  extracts) and is packed identically in memory.

### 3. Packed POI index

- Same trick: parallel vocab-coded columns instead of a dict per place
  (~46 MB → ~20 MB), and the unused place id was dropped.

### 4. API serving details

- `warm()` prefers the bundle and only falls back to parsing raw slices when
  it's absent (the fallback is what the test fixtures exercise, so both paths
  are continuously tested).
- The `/data/*.geojson` endpoints stream from disk with `FileResponse`
  instead of a 39 MB `read_bytes()` spike per request, and `GZipMiddleware`
  cuts the map's initial download from 39 MB to 8.4 MB over the wire.
- `sync.py` emits all bundle artifacts after fetching, so Render's build
  command (`python -m pev_buddy.sync`) regenerates them on every deploy with
  no config changes.

## Results

| Metric | Before | After |
|---|---|---|
| Steady-state RSS | ~960 MB | **~316 MB** |
| Boot peak | ~960 MB | **~350 MB** |
| Warm-up time | 1.6 s | **0.43 s** |
| Cross-city route latency | 129 ms | **108 ms** |

Fits Render's 512 MB free tier with ~200 MB of headroom.

## How we verified no functionality was lost

- All 85 pre-existing tests pass unchanged (they exercise the raw-slice
  fallback path — the compatibility guarantee).
- New `tests/test_bundle.py`: bundle round-trip equality (edges, segments,
  masks, SCC), route and turn-by-turn identity between built and
  bundle-loaded graphs, and full API parity between bundle and non-bundle
  apps.
- Before touching any code, we captured real API responses (health, search,
  geocode, routes for three vehicle types) from the original implementation
  and diffed them against the refactored one: **byte-identical**.
- Memory claims were measured with `tracemalloc` (retained Python objects,
  not just RSS, which over-reports on macOS due to allocator behavior).
