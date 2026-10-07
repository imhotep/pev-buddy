"use strict";

const SF_CENTER = [-122.4194, 37.7749];
// Users who prefer reduced motion get instant map jumps instead of animated
// flyTo/fitBounds transitions.
const REDUCED_MOTION = matchMedia("(prefers-reduced-motion: reduce)").matches;
const FLY = REDUCED_MOTION ? { duration: 0 } : {};
// Mobile-only navigation aids: live location dot, heading-up map rotation,
// and a screen wake lock while a route is up. Desktop gets none of these.
const IS_MOBILE = matchMedia("(pointer: coarse)").matches;
// City of San Francisco extent — the map can't pan or zoom out beyond this.
const SF_BOUNDS = [
  [-122.52, 37.7],
  [-122.35, 37.84],
];

const map = new maplibregl.Map({
  container: "map",
  style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  center: SF_CENTER,
  zoom: 12.5,
  // maxBounds clamps zoom-out at the city's extent; minZoom is kept well below
  // that so it never overrides the bounds on wide viewports.
  minZoom: 9,
  maxBounds: SF_BOUNDS,
  attributionControl: false,
});
map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
map.addControl(new maplibregl.NavigationControl(), "top-right");

// Live location dot (with heading cone) — mobile only, where it matters for
// actually riding the route.
let geolocateControl = null;
if (IS_MOBILE) {
  geolocateControl = new maplibregl.GeolocateControl({
    positionOptions: { enableHighAccuracy: true },
    trackUserLocation: true,
    showUserLocation: true,
  });
  map.addControl(geolocateControl, "top-right");
}
// MapLibre labels the canvas just "Map" — give assistive tech a real summary.
// Keyboard pan/zoom (+/-/arrows) is built into the canvas itself.
map.getCanvas().setAttribute(
  "aria-label",
  "Interactive map of San Francisco showing EV chargers, BikeLink lockers, and the current route"
);

const state = {
  startRef: null, // {kind, station_id?, query?, lat, lon, label}
  endRef: null,
  routeShown: false, // a route is currently drawn on the map
  activeStep: null, // index of the step highlighted on the map
  vehicle: "scooter", // CA vehicle type id (from /api/vehicles)
};

// ---------------------------------------------------------------- vehicle type

let vehicleTypes = []; // {id, label, description, routable, speed_limit_mph}

// The dropdown and its live description both come from /api/vehicles, which
// serves config.VEHICLE_TYPES — the single source of truth for vehicle types.
async function loadVehicleTypes() {
  const res = await fetch("/api/vehicles");
  if (!res.ok) return;
  const data = await res.json();
  vehicleTypes = data.vehicles;
  const sel = document.getElementById("vehicle");
  for (const v of data.vehicles) {
    const opt = document.createElement("option");
    opt.value = v.id;
    opt.textContent = v.label;
    sel.appendChild(opt);
  }
  state.vehicle = data.default;
  sel.value = data.default;
  syncVehicleDesc();
}

function syncVehicleDesc() {
  const v = vehicleTypes.find((x) => x.id === state.vehicle);
  document.getElementById("vehicle-desc").textContent = v ? v.description : "";
}

document.getElementById("vehicle").addEventListener("change", (e) => {
  state.vehicle = e.target.value;
  syncVehicleDesc();
  maybeRoute();
});
loadVehicleTypes();

// Imperial everywhere (matches the API's warning text, geo.format_distance):
// feet under a tenth of a mile, so a short step never reads "0.0 mi".
const M_PER_MILE = 1609.344;
const FT_PER_M = 3.28084;
function fmtDist(m) {
  if (m < 0.1 * M_PER_MILE) return `${Math.round((m * FT_PER_M) / 10) * 10} ft`;
  return `${(m / M_PER_MILE).toFixed(1)} mi`;
}
const fmtMin = (s) => `${Math.max(1, Math.round(s / 60))} min`;

// ---------------------------------------------------------------- map layers

map.on("load", async () => {
  let roads, pois;
  try {
    roads = await (await fetch("/data/roads.geojson")).json();
    const stations = await (await fetch("/data/stations.geojson")).json();
    // BikeLink lockers and SFMTA racks are optional slices — a missing file
    // just means fewer pins.
    const bikelink = await fetchFC("/data/bikelink.geojson");
    const racks = await fetchFC("/data/racks.geojson");
    // Chargers, lockers, and racks share one clustered source: zoomed out the
    // map shows count bubbles instead of thousands of overlapping pins.
    pois = {
      type: "FeatureCollection",
      features: [
        ...stations.features.map((f) => withKind(f, "charger")),
        ...bikelink.features.map((f) => withKind(f, "locker")),
        ...racks.features.map((f) => withKind(f, "rack")),
      ],
    };
  } catch {
    const el = document.getElementById("map-loading");
    if (el) el.textContent = "Map data failed to load — reload to try again.";
    return;
  }

  map.addSource("pev-roads", { type: "geojson", data: roads });
  map.addLayer({
    id: "pev-roads",
    type: "line",
    source: "pev-roads",
    paint: {
      "line-color": [
        "match",
        ["get", "class"],
        "cycleway", "#35d07f",
        "path", "#35d07f",
        "track", "#35d07f",
        "footway", "#4a5d70",
        "residential", "#5d7086",
        "living_street", "#5d7086",
        "service", "#526275",
        "unclassified", "#526275",
        "tertiary", "#46566a",
        "#333f4e",
      ],
      "line-width": [
        "match",
        ["get", "class"],
        "cycleway", 2.6,
        "path", 2.6,
        "track", 2.2,
        1.4,
      ],
      "line-opacity": 0.85,
    },
  });

  map.addSource("pev-route-casing", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "pev-route-casing",
    type: "line",
    source: "pev-route-casing",
    paint: { "line-color": "#0c141d", "line-width": 10, "line-opacity": 0.95 },
  });
  map.addSource("pev-route", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "pev-route",
    type: "line",
    source: "pev-route",
    paint: { "line-color": "#ffb02e", "line-width": 5.5, "line-opacity": 0.98 },
  });

  // Highlight for the step selected in the turn-by-turn list.
  map.addSource("pev-step-hl", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "pev-step-hl",
    type: "line",
    source: "pev-step-hl",
    paint: { "line-color": "#ffffff", "line-width": 7, "line-opacity": 0.95 },
  });
  map.addSource("pev-step-dot", { type: "geojson", data: emptyFC() });
  map.addLayer({
    id: "pev-step-dot",
    type: "circle",
    source: "pev-step-dot",
    paint: {
      "circle-radius": 7,
      "circle-color": "#ffffff",
      "circle-stroke-width": 3,
      "circle-stroke-color": "#ffb02e",
    },
  });

  map.addSource("pev-pois", {
    type: "geojson",
    data: pois,
    promoteId: "id",
    cluster: true,
    // Below this zoom every pin is shown individually; at or above it, nearby
    // pins collapse into a count bubble.
    clusterMaxZoom: 14,
    clusterRadius: 55,
  });
  map.addLayer({
    id: "pev-clusters",
    type: "circle",
    source: "pev-pois",
    filter: ["has", "point_count"],
    paint: {
      "circle-color": "#e8eef5",
      "circle-radius": ["step", ["get", "point_count"], 16, 25, 20, 100, 26],
      "circle-stroke-width": 2.5,
      "circle-stroke-color": "#0c141d",
    },
  });
  map.addLayer({
    id: "pev-cluster-count",
    type: "symbol",
    source: "pev-pois",
    filter: ["has", "point_count"],
    layout: {
      "text-field": ["get", "point_count_abbreviated"],
      "text-font": ["Open Sans Bold"],
      "text-size": 13,
    },
    paint: { "text-color": "#0c141d" },
  });
  map.addLayer({
    id: "pev-pois",
    type: "circle",
    source: "pev-pois",
    filter: ["!", ["has", "point_count"]],
    paint: {
      // charger amber, locker blue, rack violet
      "circle-color": [
        "match", ["get", "kind"],
        "charger", "#ffb02e",
        "locker", "#4da3ff",
        "rack", "#b48cff",
        "#ffb02e",
      ],
      // Racks number in the thousands — keep their pins smaller.
      "circle-radius": ["match", ["get", "kind"], "rack", 5, 9],
      "circle-stroke-width": 2,
      "circle-stroke-color": "#0c141d",
    },
  });

  const clickable = ["pev-pois", "pev-clusters"];
  map.on("mousemove", (e) => {
    const over = map.queryRenderedFeatures(e.point, { layers: clickable }).length > 0;
    map.getCanvas().style.cursor = over ? "pointer" : "";
  });

  const loading = document.getElementById("map-loading");
  if (loading) loading.remove();
});

async function fetchFC(url) {
  const res = await fetch(url);
  return res.ok ? await res.json() : emptyFC();
}

function withKind(f, kind) {
  return { ...f, properties: { ...f.properties, kind } };
}

function emptyFC() {
  return { type: "FeatureCollection", features: [] };
}

// Start/destination pins. Drag one to move that endpoint; the route follows.
const startMarker = new maplibregl.Marker({ element: makeEndpointPin("start"), draggable: true });
const endMarker = new maplibregl.Marker({ element: makeEndpointPin("end"), draggable: true });
startMarker.on("dragend", () => setStart(pinRef(startMarker.getLngLat())));
endMarker.on("dragend", () => setEnd(pinRef(endMarker.getLngLat())));

function makeEndpointPin(which) {
  // A 44px touch target around the visible dot, so it can be grabbed with a
  // finger. Decorative to assistive tech — the sidebar labels carry the info.
  const el = document.createElement("div");
  el.className = `endpoint-pin ${which}`;
  el.setAttribute("aria-hidden", "true");
  return el;
}

// A point picked on the map (tap or drag). It reads "Dropped pin" until
// /api/reverse names the nearest address ("Near 123 Valencia St"); `labeled`
// resolves once that lookup settles.
function pinRef(lngLat) {
  const ref = { kind: "pin", lat: lngLat.lat, lon: lngLat.lng, label: "Dropped pin", address: null };
  ref.labeled = labelPin(ref);
  return ref;
}

async function labelPin(ref) {
  try {
    const res = await fetch(`/api/reverse?lat=${ref.lat}&lon=${ref.lon}`);
    if (!res.ok) return;
    const r = await res.json();
    ref.label = r.label;
    ref.address = r.address;
    syncTripLabels();
  } catch {
    // Offline or data not loaded yet: "Dropped pin" stands.
  }
}

// "Start here" / "Go here" buttons shared by every map bubble, so a tap on a
// pin is never a dead end: whatever was tapped can become either endpoint.
function pickActions(popup, ref) {
  const row = document.createElement("div");
  row.className = "bubble-actions";
  for (const [text, cls, set] of [
    ["Start here", "bubble-start", setStart],
    ["Go here", "bubble-route", setEnd],
  ]) {
    const b = document.createElement("button");
    b.className = cls;
    b.textContent = text;
    b.addEventListener("click", () => {
      popup.remove();
      set(ref);
    });
    row.appendChild(b);
  }
  return row;
}

// Tap on empty map: offer the spot as start or destination. Works while a
// route is shown too — the choice replaces that endpoint and reroutes.
function showPickPopup(lngLat) {
  const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, offset: 8 }).setLngLat(lngLat);
  const card = document.createElement("div");
  card.className = "bubble pick";
  const title = document.createElement("div");
  title.className = "bubble-title";
  title.textContent = "Dropped pin";
  const ref = pinRef(lngLat);
  ref.labeled.then(() => (title.textContent = ref.label));
  card.append(title, pickActions(popup, ref));
  popup.setDOMContent(card).addTo(map);
  return popup;
}

// ---------------------------------------------------------------- stations

// Build a details "bubble" for a charger and let the user navigate to it.
function showStationPopup(s) {
  const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, offset: 14 }).setLngLat([s.lon, s.lat]);

  const card = document.createElement("div");
  card.className = "bubble";

  const title = document.createElement("div");
  title.className = "bubble-title";
  title.textContent = s.name || "Charging station";

  const meta = document.createElement("div");
  meta.className = "bubble-meta";
  const bits = [];
  if (s.brand) bits.push(s.brand);
  if (s.address) bits.push(s.address);
  if (s.phone) bits.push(s.phone);
  if (bits.length) meta.textContent = bits.join(" · ");
  else meta.textContent = "No details available";

  card.append(title, meta);

  if (s.website) {
    const a = document.createElement("a");
    a.className = "bubble-link";
    a.href = s.website;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "Charger info ↗";
    card.appendChild(a);
  }

  const label = s.name || "Charging station";
  // Prefer the station id; fall back to coordinates if it is unavailable.
  const ref = s.id
    ? { kind: "station", station_id: s.id, lat: s.lat, lon: s.lon, label }
    : { kind: "coords", lat: s.lat, lon: s.lon, label };
  card.appendChild(pickActions(popup, ref));

  popup.setDOMContent(card).addTo(map);
  return popup;
}

// MapLibre stringifies array properties on rendered features, so a list that
// went out as JSON comes back as a JSON string — normalize either shape.
function asStringArray(v) {
  if (Array.isArray(v)) return v;
  if (typeof v === "string") {
    try {
      const a = JSON.parse(v);
      return Array.isArray(a) ? a : [];
    } catch {
      return [];
    }
  }
  return [];
}

// Build a details "bubble" for a BikeLink locker and let the user navigate to it.
function showBikeLinkPopup(b) {
  b.access_devices = asStringArray(b.access_devices);
  const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, offset: 14 }).setLngLat([b.lon, b.lat]);

  const card = document.createElement("div");
  card.className = "bubble";

  const title = document.createElement("div");
  title.className = "bubble-title";
  title.textContent = b.name || "BikeLink locker";

  const meta = document.createElement("div");
  meta.className = "bubble-meta";
  const bits = [];
  if (b.facility_type) bits.push(b.facility_type);
  if (b.address) bits.push(b.address);
  if (b.num_spaces) bits.push(`${b.num_spaces} ${b.facility_type === "eLocker" ? "lockers" : "spaces"}`);
  if (bits.length) meta.textContent = bits.join(" · ");
  else meta.textContent = "No details available";

  card.append(title, meta);

  if (b.access_devices && b.access_devices.length) {
    const access = document.createElement("div");
    access.className = "bubble-meta";
    access.textContent = `Access: ${b.access_devices.join(", ")}`;
    card.appendChild(access);
  }

  const label = b.name || "BikeLink locker";
  const ref = b.id
    ? { kind: "bikelink", station_id: b.id, lat: b.lat, lon: b.lon, label }
    : { kind: "coords", lat: b.lat, lon: b.lon, label };
  card.appendChild(pickActions(popup, ref));

  popup.setDOMContent(card).addTo(map);
  return popup;
}

// Build a details bubble for an SFMTA bike rack (sidewalk racks and corrals).
function showRackPopup(r) {
  const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, offset: 14 }).setLngLat([r.lon, r.lat]);

  const card = document.createElement("div");
  card.className = "bubble";

  const title = document.createElement("div");
  title.className = "bubble-title";
  title.textContent = r.name || "Bike rack";

  const meta = document.createElement("div");
  meta.className = "bubble-meta";
  const bits = [];
  if (r.landmark) bits.push(r.landmark);
  if (r.spaces) bits.push(`${r.spaces} spaces`);
  else if (r.racks) bits.push(`${r.racks} rack${r.racks === 1 ? "" : "s"}`);
  if (r.placement) bits.push(`${r.placement} placement`);
  if (r.install_yr) bits.push(`installed ${r.install_yr}`);
  meta.textContent = bits.length ? bits.join(" · ") : "No details available";

  card.append(title, meta);

  // The router doesn't know rack ids — route to the coordinates.
  card.appendChild(pickActions(popup, { kind: "coords", lat: r.lat, lon: r.lon, label: r.name || "Bike rack" }));

  popup.setDOMContent(card).addTo(map);
  return popup;
}

// ---------------------------------------------------------------- trip refs

const DEFAULT_START_LABEL = "Not set — use your location, search, or tap the map and choose “Start here”";
const DEFAULT_END_LABEL = "Not set — search, or tap the map or a pin and choose “Go here”";

function setStart(ref) {
  state.startRef = ref;
  startMarker.setLngLat([ref.lon, ref.lat]).addTo(map);
  syncTripLabels();
  maybeRoute();
}

function setEnd(ref) {
  state.endRef = ref;
  endMarker.setLngLat([ref.lon, ref.lat]).addTo(map);
  syncTripLabels();
  maybeRoute();
}

// Start/Destination labels, plus the empty-state cue: the next row that
// needs filling in (Start first) is highlighted.
function syncTripLabels() {
  const { startRef, endRef } = state;
  document.getElementById("start-label").textContent = startRef ? startRef.label : DEFAULT_START_LABEL;
  document.getElementById("end-label").textContent = endRef ? endRef.label : DEFAULT_END_LABEL;
  document.getElementById("start-row").classList.toggle("needs-input", !startRef);
  document.getElementById("end-row").classList.toggle("needs-input", !!startRef && !endRef);
}
syncTripLabels();

function maybeRoute() {
  if (state.startRef && state.endRef) computeRoute();
}

// Clear everything: start/end, markers, route line, step highlight, and the
// steps drawer.
function resetTrip() {
  cancelRouting();
  state.startRef = null;
  state.endRef = null;
  state.routeShown = false;
  state.activeStep = null;
  startMarker.remove();
  endMarker.remove();
  syncTripLabels();
  showTripStatus("");
  if (map.getSource("pev-route")) map.getSource("pev-route").setData(emptyFC());
  if (map.getSource("pev-route-casing")) map.getSource("pev-route-casing").setData(emptyFC());
  clearStepHighlight();
  document.getElementById("route-summary").classList.add("hidden");
  document.getElementById("steps-drawer").classList.add("hidden");
  syncStepsToggle();
  syncNavUI();
  // Leaving navigation mode: face north again and let the screen sleep.
  map.easeTo({ bearing: 0, ...FLY });
  releaseWakeLock();
}
document.getElementById("reset-route").addEventListener("click", resetTrip);

// While a route is on the map but the steps drawer is hidden, offer a way
// back to the steps.
function syncStepsToggle() {
  const drawerHidden = document.getElementById("steps-drawer").classList.contains("hidden");
  const toggle = document.getElementById("show-steps");
  toggle.classList.toggle("hidden", !(state.routeShown && drawerHidden));
  toggle.setAttribute("aria-expanded", String(!drawerHidden));
}

function openStepsDrawer(moveFocus) {
  document.getElementById("steps-drawer").classList.remove("hidden");
  syncStepsToggle();
  if (moveFocus) document.getElementById("steps-close").focus();
}

function closeStepsDrawer(moveFocus) {
  document.getElementById("steps-drawer").classList.add("hidden");
  syncStepsToggle();
  // Return focus to the toggle that reopens the drawer, if it is on screen.
  const toggle = document.getElementById("show-steps");
  if (moveFocus && !toggle.classList.contains("hidden")) toggle.focus();
}

document.getElementById("show-steps").addEventListener("click", () => openStepsDrawer(true));

// In-page status line under the trip rows (replaces blocking alerts).
function showTripStatus(text, isError = false) {
  const el = document.getElementById("trip-status");
  el.textContent = text;
  el.classList.toggle("error", isError);
  el.classList.toggle("hidden", !text);
}

const LOCATION_ERRORS = {
  1: "Location permission is off for this site. Allow it in your browser's site settings, or search / tap the map instead.",
  2: "Your position isn't available right now. Check that Location Services are on, or search / tap the map instead.",
  3: "Timed out finding your location. Try again, or search / tap the map instead.",
};

// Fill the start or destination with the device position, reporting every
// outcome in the page: in progress, success, and each failure mode.
function useMyLocation(which) {
  if (!window.isSecureContext) {
    return showTripStatus("Location needs a secure (https) connection. Search or tap the map instead.", true);
  }
  if (!navigator.geolocation) {
    return showTripStatus("This browser can't share your location. Search or tap the map instead.", true);
  }
  const button = document.getElementById(which === "end" ? "use-location-end" : "use-location");
  button.disabled = true;
  showTripStatus("Finding your location…");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      button.disabled = false;
      const { latitude: lat, longitude: lon } = pos.coords;
      if (!inSF(lon, lat)) {
        showTripStatus("You appear to be outside San Francisco. PEV Buddy only routes within the city.", true);
        return;
      }
      showTripStatus("");
      const ref = { kind: "coords", lat, lon, label: "My location" };
      if (which === "end") setEnd(ref);
      else setStart(ref);
      map.flyTo({ center: [lon, lat], zoom: 14, ...FLY });
    },
    (err) => {
      button.disabled = false;
      showTripStatus(LOCATION_ERRORS[err.code] || "Could not get your location. Search or tap the map instead.", true);
    },
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 }
  );
}

function inSF(lon, lat) {
  const [[w, s], [e, n]] = SF_BOUNDS;
  return lon >= w && lon <= e && lat >= s && lat <= n;
}
document.getElementById("use-location").addEventListener("click", () => useMyLocation("start"));
document.getElementById("use-location-end").addEventListener("click", () => useMyLocation("end"));

// ---------------------------------------------------------------- search

const searchInput = document.getElementById("search");
const searchResults = document.getElementById("search-results");
const searchStatus = document.getElementById("search-status");
let searchTimer = null;

function showSearchResults() {
  searchResults.classList.remove("hidden");
  searchInput.setAttribute("aria-expanded", "true");
}

function hideSearchResults() {
  searchResults.classList.add("hidden");
  searchInput.setAttribute("aria-expanded", "false");
  searchStatus.textContent = "";
}

searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();
  if (q.length < 2) {
    hideSearchResults();
    return;
  }
  searchTimer = setTimeout(() => runSearch(q), 300);
});

// Escape anywhere inside the search area closes the dropdown and returns
// focus to the input.
document.querySelector(".search").addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || searchResults.classList.contains("hidden")) return;
  hideSearchResults();
  searchInput.focus();
});

async function runSearch(q) {
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}&limit=8`);
    if (!res.ok) throw new Error();
    const items = await res.json();
    if (!items.length) {
      searchResults.innerHTML = `<div class="result muted">No matches</div>`;
      showSearchResults();
      searchStatus.textContent = "No matches";
      return;
    }
    searchResults.innerHTML = "";
    for (const r of items) {
      const div = document.createElement("div");
      div.className = "result";
      const label = document.createElement("span");
      label.textContent = r.text;
      // "Category · address" so same-named places (eight "Ferry Building…"
      // hits) can be told apart.
      const sub = document.createElement("small");
      sub.textContent = [r.category || "Place", r.address].filter(Boolean).join(" · ");
      div.append(label, sub);
      const row = document.createElement("div");
      row.style.cssText = "display:flex;gap:6px;margin-top:6px;";
      for (const [text, kind] of [["Start", "start"], ["Destination", "end"]]) {
        const b = document.createElement("button");
        b.className = "mini";
        b.textContent = text;
        b.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const ref = {
            kind: r.kind === "station" || r.kind === "bikelink" ? r.kind : "point",
            station_id: r.station_id || null,
            label: r.text,
            lat: r.lat,
            lon: r.lon,
          };
          if (kind === "start") setStart(ref);
          else setEnd(ref);
          map.flyTo({ center: [r.lon, r.lat], zoom: 15, ...FLY });
          // The pick now lives in the Start/Destination row; clear the box so
          // it is ready for the next search.
          searchInput.value = "";
          hideSearchResults();
        });
        row.appendChild(b);
      }
      div.appendChild(row);
      div.addEventListener("click", () => {
        map.flyTo({ center: [r.lon, r.lat], zoom: 15, ...FLY });
        hideSearchResults();
      });
      searchResults.appendChild(div);
    }
    showSearchResults();
    searchStatus.textContent = `${items.length} result${items.length === 1 ? "" : "s"}`;
  } catch {
    hideSearchResults();
  }
}

document.addEventListener("click", (e) => {
  if (!e.target.closest(".search")) hideSearchResults();
});

// map click: a pin shows its bubble; a count bubble zooms in (handled by the
// pev-clusters handler below); empty space opens a "Start here / Go here"
// popup. Every bubble offers both, so no tap is a dead end, and a route on
// the map can be edited the same way (POI pins are hidden while it is).
map.on("click", (e) => {
  // Cluster clicks are the zoom handler's job — don't treat them as empty space.
  if (map.queryRenderedFeatures(e.point, { layers: ["pev-clusters"] }).length) return;
  const feats = map.queryRenderedFeatures(e.point, { layers: ["pev-pois"] });
  if (feats.length) {
    const f = feats[0];
    const p = f.properties || {};
    const ll = { lat: f.geometry.coordinates[1], lon: f.geometry.coordinates[0] };
    if (p.kind === "locker") {
      showBikeLinkPopup({
        id: f.id,
        name: p.name,
        facility_type: p.facility_type,
        address: p.address,
        num_spaces: p.num_spaces,
        access_devices: p.access_devices,
        ...ll,
      });
    } else if (p.kind === "rack") {
      showRackPopup({
        id: f.id,
        name: p.name,
        landmark: p.landmark,
        placement: p.placement,
        racks: p.racks,
        spaces: p.spaces,
        install_yr: p.install_yr,
        ...ll,
      });
    } else {
      showStationPopup({
        id: f.id,
        name: p.name,
        brand: p.brand,
        address: p.address,
        phone: p.phone,
        website: p.website,
        ...ll,
      });
    }
    return;
  }
  showPickPopup(e.lngLat);
});

// Click a count bubble: zoom in until it breaks apart.
map.on("click", "pev-clusters", (e) => {
  const f = map.queryRenderedFeatures(e.point, { layers: ["pev-clusters"] })[0];
  if (!f) return;
  map
    .getSource("pev-pois")
    .getClusterExpansionZoom(f.properties.cluster_id)
    .then((zoom) => map.easeTo({ center: f.geometry.coordinates, zoom, ...FLY }));
});

// ------------------------------------------------------- mobile navigation

// Page pinch-zoom guard: two-finger zoom belongs to the map only. The
// viewport meta + touch-action: manipulation cover Android; iOS Safari
// ignores those, so we cancel its gesturestart event. Touches that begin on
// the map canvas are always exempt — MapLibre handles the pinch itself.
// (A document-level touchmove guard was tried and broke map pinch on iOS.)
document.addEventListener("gesturestart", (e) => {
  if (!(e.target instanceof Element) || !e.target.closest("#map")) e.preventDefault();
});

// While a route is up on a phone, the panel tucks away and a cancel button
// is the way back to it.
function syncNavUI() {
  const nav = IS_MOBILE && state.routeShown;
  document.body.classList.toggle("navigating", nav);
  document.getElementById("cancel-route").classList.toggle("hidden", !nav);
  // Declutter the map while navigating: POI pins and count bubbles go away.
  for (const id of ["pev-pois", "pev-clusters", "pev-cluster-count"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", state.routeShown ? "none" : "visible");
  }
}
document.getElementById("cancel-route").addEventListener("click", resetTrip);

// iOS 13+ requires a user gesture for compass access — ask on the first tap.
function ensureOrientation() {
  if (!IS_MOBILE || orientBound) return;
  if (typeof DeviceOrientationEvent !== "undefined" && typeof DeviceOrientationEvent.requestPermission === "function") {
    DeviceOrientationEvent.requestPermission()
      .then((p) => {
        if (p === "granted") bindOrientation();
      })
      .catch(() => {});
  } else {
    bindOrientation();
  }
}

// Heading-up rotation: while a route is shown, the map turns with the rider
// so the upcoming turn is always "straight ahead". iOS gives compass heading
// directly; elsewhere derive it from the absolute alpha angle.
let orientBound = false;
function bindOrientation() {
  if (orientBound) return;
  orientBound = true;
  const handler = (e) => {
    if (!state.routeShown) return;
    let hdg = null;
    if (typeof e.webkitCompassHeading === "number") hdg = e.webkitCompassHeading;
    else if (e.absolute && typeof e.alpha === "number") hdg = 360 - e.alpha;
    if (hdg === null || Number.isNaN(hdg)) return;
    map.easeTo({ bearing: hdg, duration: 300 });
  };
  window.addEventListener("deviceorientationabsolute", handler, true);
  window.addEventListener("deviceorientation", handler, true);
}
if (IS_MOBILE) window.addEventListener("pointerdown", ensureOrientation);

// Keep the screen awake while navigating (Screen Wake Lock API).
let wakeLock = null;
async function acquireWakeLock() {
  if (!IS_MOBILE || !("wakeLock" in navigator) || wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request("screen");
  } catch {
    /* denied or unsupported — non-fatal */
  }
}
function releaseWakeLock() {
  if (wakeLock) wakeLock.release().catch(() => {});
  wakeLock = null;
}
// The lock is dropped when the tab hides; re-acquire on return.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.routeShown) acquireWakeLock();
});

// ---------------------------------------------------------------- routing

// The in-flight /api/route request, if any. Only the latest request may
// render: a new one aborts its predecessor and Reset aborts it outright, so a
// slow response for an old start/destination/vehicle can never paint a stale
// route over the current trip.
let routeRequest = null;

function cancelRouting() {
  if (routeRequest) routeRequest.abort();
  routeRequest = null;
}

async function computeRoute() {
  cancelRouting();
  const request = new AbortController();
  routeRequest = request;
  const summary = document.getElementById("route-summary");
  summary.classList.remove("hidden");
  summary.textContent = "Finding your route…";

  const body = {
    start: refToApi(state.startRef),
    end: refToApi(state.endRef),
    vehicle: state.vehicle,
  };
  try {
    const res = await fetch("/api/route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: request.signal,
    });
    const data = await res.json();
    if (request !== routeRequest) return; // superseded while the body streamed in
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    renderRoute(data);
  } catch (err) {
    if (err.name === "AbortError" || request !== routeRequest) return;
    state.routeShown = false;
    syncNavUI();
    summary.innerHTML = `<span class="warn">⚠ ${escapeHtml(err.message)}</span>`;
    closeStepsDrawer(false);
  } finally {
    if (request === routeRequest) routeRequest = null;
  }
}

function refToApi(ref) {
  if (!ref) return {};
  if ((ref.kind === "station" || ref.kind === "bikelink") && ref.station_id) {
    return { station_id: ref.station_id };
  }
  const out = { lat: ref.lat, lon: ref.lon };
  // A map pin names its matched address (so the last step reads "Arrive at
  // 123 Valencia St."), never "Near …" or "Dropped pin".
  const label = ref.kind === "pin" ? ref.address : ref.label;
  if (label) out.label = label;
  return out;
}

function renderRoute(data) {
  state.routeShown = true;
  syncNavUI();
  if (IS_MOBILE) {
    // Start tracking the rider and keep the screen on for the ride.
    if (geolocateControl) geolocateControl.trigger();
    acquireWakeLock();
  }
  const fc = {
    type: "Feature",
    geometry: { type: "LineString", coordinates: data.path },
  };
  map.getSource("pev-route").setData({ type: "FeatureCollection", features: [fc] });
  map.getSource("pev-route-casing").setData({ type: "FeatureCollection", features: [fc] });

  const summary = document.getElementById("route-summary");
  let html = `<b>${fmtDist(data.distance_m)}</b> · ${fmtMin(data.duration_s)} · ${data.steps.length} steps<br>`;
  html += `<span style="color:var(--muted)">From ${escapeHtml(data.start_label || "start")} to ${escapeHtml(data.end_label || "destination")}</span>`;
  for (const w of data.warnings || []) html += `<div class="warn">⚠ ${escapeHtml(w)}</div>`;
  summary.innerHTML = html;

  clearStepHighlight();
  const ol = document.getElementById("steps");
  ol.innerHTML = "";
  for (const s of data.steps) {
    const li = document.createElement("li");
    if (s.maneuver === "arrive") li.className = "arrive";
    const idx = document.createElement("span");
    idx.className = "idx";
    idx.textContent = s.index;
    const txt = document.createElement("span");
    txt.className = "txt";
    const main = document.createElement("span");
    main.textContent = s.instruction;
    txt.appendChild(main);
    // Show the road name whenever the instruction does not already contain it
    // (e.g. the arrival step).
    if (s.road && !s.instruction.includes(s.road)) {
      const sub = document.createElement("span");
      sub.className = "txt-road";
      sub.textContent = s.road;
      txt.appendChild(sub);
    }
    const dist = document.createElement("span");
    dist.className = "dist";
    // Depart/arrive and zero-length legs show no distance rather than "0 ft".
    dist.textContent = s.maneuver === "depart" || s.maneuver === "arrive" || !s.distance_m ? "" : fmtDist(s.distance_m);
    li.append(idx, txt, dist);
    li.addEventListener("click", () => selectStep(li, s));
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.setAttribute("aria-pressed", "false");
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectStep(li, s);
      }
    });
    ol.appendChild(li);
  }
  document.getElementById("steps-title").textContent = "Turn-by-turn";
  openStepsDrawer(false);
  // Fit after the drawer opens so the padding accounts for it.
  fitToPath(data.path, { duration: REDUCED_MOTION ? 0 : 600 });
}

// Zoom the camera to a [lon, lat] polyline. Bounds come from the path alone —
// extending the current view would mean never zooming in — and the padding
// keeps it clear of whatever UI currently covers the map.
function fitToPath(path, options = {}) {
  if (!path || !path.length) return;
  const bounds = new maplibregl.LngLatBounds(path[0], path[0]);
  for (const c of path) bounds.extend(c);
  map.fitBounds(bounds, { padding: mapPadding(), ...options });
}

// Padding (px) that keeps fitted content clear of the panels overlaying the
// map (the bottom sheet on phones, the steps drawer). Each overlay pads the
// edge it hugs, measured from the live layout so it is right at any viewport
// size (a fixed 380px left inset is wider than a phone).
const MAP_OVERLAYS = ["sidebar", "steps-drawer"];
function mapPadding(gap = 40) {
  const m = map.getContainer().getBoundingClientRect();
  const pad = { top: gap, right: gap, bottom: gap, left: gap };
  for (const id of MAP_OVERLAYS) {
    const el = document.getElementById(id);
    if (!el || !el.getClientRects().length) continue; // not rendered
    const r = el.getBoundingClientRect();
    const w = Math.min(r.right, m.right) - Math.max(r.left, m.left);
    const h = Math.min(r.bottom, m.bottom) - Math.max(r.top, m.top);
    if (w <= 0 || h <= 0) continue; // beside the map, not over it (desktop sidebar)
    if (w > m.width / 2) {
      // Spans the map's width: a top banner or a bottom sheet.
      if (r.top - m.top < m.bottom - r.bottom) pad.top = Math.max(pad.top, r.bottom - m.top + gap);
      else pad.bottom = Math.max(pad.bottom, m.bottom - r.top + gap);
    } else if (r.left - m.left < m.right - r.right) {
      pad.left = Math.max(pad.left, r.right - m.left + gap);
    } else {
      pad.right = Math.max(pad.right, m.right - r.left + gap);
    }
  }
  // Padding must leave some map to fit into: shrink proportionally if the
  // overlays leave less than 80px on an axis.
  for (const [a, b, size] of [["left", "right", m.width], ["top", "bottom", m.height]]) {
    const room = size - 80;
    if (pad[a] + pad[b] > room) {
      const k = room / (pad[a] + pad[b]);
      pad[a] = Math.floor(pad[a] * k);
      pad[b] = Math.floor(pad[b] * k);
    }
  }
  return pad;
}

// ---------------------------------------------------------------- step highlight

function clearStepHighlight() {
  state.activeStep = null;
  if (map.getSource("pev-step-hl")) map.getSource("pev-step-hl").setData(emptyFC());
  if (map.getSource("pev-step-dot")) map.getSource("pev-step-dot").setData(emptyFC());
  document.querySelectorAll("#steps li.active").forEach((li) => {
    li.classList.remove("active");
    li.setAttribute("aria-pressed", "false");
  });
}

// Click a step in the list: highlight the segment it covers on the map
// (click the same step again to clear).
function selectStep(li, step) {
  if (state.activeStep === step.index) {
    clearStepHighlight();
    return;
  }
  clearStepHighlight();
  state.activeStep = step.index;
  li.classList.add("active");
  li.setAttribute("aria-pressed", "true");

  const line =
    step.geometry && step.geometry.length > 1
      ? [{ type: "Feature", geometry: { type: "LineString", coordinates: step.geometry } }]
      : [];
  const dots = [];
  if (step.turn_point) dots.push({ type: "Feature", geometry: { type: "Point", coordinates: step.turn_point } });
  else if (step.geometry && step.geometry.length)
    dots.push({ type: "Feature", geometry: { type: "Point", coordinates: step.geometry[step.geometry.length - 1] } });

  map.getSource("pev-step-hl").setData({ type: "FeatureCollection", features: line });
  map.getSource("pev-step-dot").setData({ type: "FeatureCollection", features: dots });

  if (step.geometry && step.geometry.length > 1) {
    fitToPath(step.geometry, { maxZoom: 17, duration: REDUCED_MOTION ? 0 : 500 });
  } else if (step.turn_point) {
    map.flyTo({ center: step.turn_point, zoom: 16, duration: REDUCED_MOTION ? 0 : 500 });
  }
}

document.getElementById("steps-close").addEventListener("click", () => closeStepsDrawer(true));
document.getElementById("steps-drawer").addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeStepsDrawer(true);
});

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// ------------------------------------------------------- welcome modal

// First-visit intro. The "Don't show this again" choice persists in
// localStorage; dismissing without it only closes the modal for this visit.
const WELCOME_KEY = "pev-welcome-dismissed";
const welcomeModal = document.getElementById("welcome-modal");
const welcomeNever = document.getElementById("welcome-never");

function closeWelcome() {
  if (welcomeNever.checked) localStorage.setItem(WELCOME_KEY, "1");
  welcomeModal.classList.add("hidden");
}

if (!localStorage.getItem(WELCOME_KEY)) {
  welcomeModal.classList.remove("hidden");
  document.getElementById("welcome-close").focus();
}
document.getElementById("welcome-close").addEventListener("click", closeWelcome);
welcomeModal.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeWelcome();
});
// Clicking the dimmed backdrop dismisses too.
welcomeModal.addEventListener("click", (e) => {
  if (e.target === welcomeModal) closeWelcome();
});

// PWA: cache the app shell for offline loads and make the app installable.
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
