"use strict";

const SF_CENTER = [-122.4194, 37.7749];
// Users who prefer reduced motion get instant map jumps instead of animated
// flyTo/fitBounds transitions.
const REDUCED_MOTION = matchMedia("(prefers-reduced-motion: reduce)").matches;
const FLY = REDUCED_MOTION ? { duration: 0 } : {};
// Touch devices get MapLibre's location button and compass heading-up while
// riding. Ride mode itself (GPS guidance, voice, wake lock) works anywhere.
const IS_MOBILE = matchMedia("(pointer: coarse)").matches;
// Phone-sized layout (the sidebar becomes a bottom sheet) — mirrors the
// max-width breakpoint in style.css.
const SMALL_SCREEN = matchMedia("(max-width: 760px)");
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
  route: null, // the last /api/route response on the map
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
  state.route = null;
  endRide();
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
}
document.getElementById("reset-route").addEventListener("click", resetTrip);

// "Steps" (route actions) and the ride bar's "Steps" both toggle the drawer.
function syncStepsToggle() {
  const drawerHidden = document.getElementById("steps-drawer").classList.contains("hidden");
  for (const toggle of document.querySelectorAll('[aria-controls="steps-drawer"]')) {
    toggle.setAttribute("aria-expanded", String(!drawerHidden));
  }
}

function openStepsDrawer(moveFocus) {
  document.getElementById("steps-drawer").classList.remove("hidden");
  syncStepsToggle();
  if (moveFocus) document.getElementById("steps-close").focus();
}

function closeStepsDrawer(moveFocus) {
  document.getElementById("steps-drawer").classList.add("hidden");
  syncStepsToggle();
  // Return focus to whichever toggle that reopens the drawer is on screen.
  const toggle = [...document.querySelectorAll('[aria-controls="steps-drawer"]')].find(
    (el) => el.getClientRects().length
  );
  if (moveFocus && toggle) toggle.focus();
}

function toggleStepsDrawer() {
  if (document.getElementById("steps-drawer").classList.contains("hidden")) openStepsDrawer(true);
  else closeStepsDrawer(true);
}
document.getElementById("show-steps").addEventListener("click", toggleStepsDrawer);

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
// Mid-ride, taps only pan, so a bump can't change the trip.
map.on("click", (e) => {
  if (ride.active) return;
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

// Route-dependent chrome: "Start ride" appears once there is a route, and
// POI pins and count bubbles step aside so the route reads clearly.
function syncNavUI() {
  document.getElementById("route-actions").classList.toggle("hidden", !state.routeShown);
  document.getElementById("sheet-toggle").classList.toggle("hidden", !state.routeShown);
  if (!state.routeShown) setSheetCollapsed(false);
  for (const id of ["pev-pois", "pev-clusters", "pev-cluster-count"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", state.routeShown ? "none" : "visible");
  }
}

// Phone bottom sheet: with a route up it collapses to the route summary and
// actions so the map stays visible; "Edit trip" expands it again. (The
// collapsed layout is a max-width rule in style.css; desktop ignores it.)
function setSheetCollapsed(collapsed) {
  document.body.classList.toggle("sheet-collapsed", collapsed);
  const toggle = document.getElementById("sheet-toggle");
  toggle.textContent = collapsed ? "▴ Edit trip" : "▾ Show map";
  toggle.setAttribute("aria-expanded", String(!collapsed));
}
document.getElementById("sheet-toggle").addEventListener("click", () => {
  setSheetCollapsed(!document.body.classList.contains("sheet-collapsed"));
  if (state.route) fitToPath(state.route.path, { duration: REDUCED_MOTION ? 0 : 400 });
});

// Compass access. iOS 13+ only grants it from a user gesture, so this runs
// from the "Start ride" tap. Touch devices only: a laptop has no compass
// worth following.
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

// Compass heading for heading-up while riding — the fallback for when the
// GPS course is unavailable (standing still, or too slow to be reliable).
// iOS gives the heading directly; elsewhere it is derived from the absolute
// alpha angle. Rotation is throttled in rotateToHeading: these events fire
// dozens of times a second, and easing on each one made the map jitter.
let orientBound = false;
function bindOrientation() {
  if (orientBound) return;
  orientBound = true;
  const handler = (e) => {
    if (!ride.active) return;
    let hdg = null;
    if (typeof e.webkitCompassHeading === "number") hdg = e.webkitCompassHeading;
    else if (e.absolute && typeof e.alpha === "number") hdg = 360 - e.alpha;
    if (hdg === null || Number.isNaN(hdg)) return;
    if (performance.now() - ride.gpsHeadingAt < GPS_HEADING_FRESH_MS) return; // moving: GPS course wins
    ride.heading = hdg;
    rotateToHeading();
  };
  window.addEventListener("deviceorientationabsolute", handler, true);
  window.addEventListener("deviceorientation", handler, true);
}

// Keep the screen awake while riding (Screen Wake Lock API).
let wakeLock = null;
async function acquireWakeLock() {
  if (!("wakeLock" in navigator) || wakeLock) return;
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
  if (document.visibilityState === "visible" && ride.active && !ride.arrived) acquireWakeLock();
});

// localStorage can be unavailable (private mode, blocked site data) or throw
// on access; preferences are a convenience, so failures fall back quietly.
function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* not persisted — the in-memory value still applies this visit */
  }
}

// ------------------------------------------------------- ride mode: logic
//
// Pure functions — no DOM, no map — so progress tracking can be tested on
// its own. Distances are meters; points are [lon, lat].

const RIDE = Object.freeze({
  OFF_ROUTE_M: 40, // farther than this from the route line is "off route"...
  OFF_ROUTE_FIXES: 3, // ...for this many fixes in a row triggers a reroute
  MAX_ACCURACY_M: 50, // vaguer fixes neither count toward nor reset off-route
  PASSED_M: 8, // a maneuver is done once the rider is this far past it
  ARRIVE_M: 20, // this close to the route's end counts as arrived
  FAR_PROMPT_M: 152, // ~500 ft: "In 500 feet, turn right onto…"
  NEAR_PROMPT_M: 30, // ~100 ft: "Turn right onto…"
  GPS_HEADING_MIN_MPS: 1.5, // below this the GPS course is noise
  BEARING_MIN_MS: 500, // rotate the map at most twice a second...
  BEARING_MIN_DEG: 6, // ...and only for a visible change
});
const GPS_HEADING_FRESH_MS = 3000;
const M_PER_DEG = 111320;

// Equirectangular projection to local meters around lat0: accurate to well
// under a meter across a city-scale route, and cheap enough to run per fix.
function toLocalXY([lon, lat], lat0) {
  return [lon * M_PER_DEG * Math.cos((lat0 * Math.PI) / 180), lat * M_PER_DEG];
}

// Precompute a route polyline for snapping: projected vertices and the
// cumulative distance at each.
function buildRouteLine(path) {
  const lat0 = path[0][1];
  const pts = path.map((p) => toLocalXY(p, lat0));
  const cum = [0];
  for (let i = 1; i < pts.length; i++) {
    cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
  }
  return { path, pts, cum, lat0, length: cum[cum.length - 1] };
}

// Closest point on the route to `p`: {distance (m off the line), along (m
// from the start), point [lon, lat], bearing of that segment}. Segments that
// end before `fromAlong` are skipped, so a route that doubles back near
// itself snaps to the stretch the rider is actually on.
function snapToRoute(line, p, fromAlong = 0) {
  const [px, py] = toLocalXY(p, line.lat0);
  const from = Math.min(Math.max(fromAlong, 0), line.length);
  let best = null;
  for (let i = 0; i < line.pts.length - 1; i++) {
    if (line.cum[i + 1] < from) continue;
    const [ax, ay] = line.pts[i];
    const [bx, by] = line.pts[i + 1];
    const dx = bx - ax;
    const dy = by - ay;
    const len2 = dx * dx + dy * dy;
    const t = len2 ? Math.min(Math.max(((px - ax) * dx + (py - ay) * dy) / len2, 0), 1) : 0;
    const distance = Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
    if (!best || distance < best.distance) best = { distance, segment: i, t };
  }
  if (!best) {
    // Single-vertex route: the start is the end.
    const [x, y] = line.pts[0];
    return { distance: Math.hypot(px - x, py - y), along: 0, point: line.path[0], bearing: null };
  }
  const { segment: i, t } = best;
  const a = line.path[i];
  const b = line.path[i + 1];
  const [ax, ay] = line.pts[i];
  const [bx, by] = line.pts[i + 1];
  return {
    distance: best.distance,
    along: line.cum[i] + t * (line.cum[i + 1] - line.cum[i]),
    point: [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])],
    bearing: ((Math.atan2(bx - ax, by - ay) * 180) / Math.PI + 360) % 360,
  };
}

// Where along the route each step's maneuver happens: the departure at 0,
// each turn at its turn_point (snapped in order, so a corner the route passes
// twice resolves to the right pass), and the arrival at the route's end.
function maneuverPositions(line, steps) {
  let from = 0;
  return steps.map((s, i) => {
    if (i === 0) return 0;
    if (s.maneuver === "arrive") return line.length;
    if (s.turn_point) from = snapToRoute(line, s.turn_point, from).along;
    return from;
  });
}

// The next maneuver ahead of a rider `along` meters into the route, and the
// distance to it. Progress only moves forward from `current`, so GPS jitter
// around a corner never flips the banner back to a turn already taken.
function nextManeuver(positions, along, current = 1) {
  const last = positions.length - 1;
  let index = Math.min(Math.max(current, 1), last);
  while (index < last && along >= positions[index] + RIDE.PASSED_M) index++;
  return { index, distance: Math.max(0, positions[index] - along) };
}

// Consecutive-fix counter for off-route detection: a fix far from the line
// counts up, a fix on it resets, and a fix too vague to judge changes nothing.
function offRouteCount(count, distance, accuracy) {
  if (accuracy > RIDE.MAX_ACCURACY_M) return count;
  return distance > RIDE.OFF_ROUTE_M ? count + 1 : 0;
}

// Which voice prompt is due for the next maneuver given how many have been
// spoken for it already: 0 none due, 1 the ~500 ft heads-up, 2 the ~100 ft
// "turn now". Reaching 100 ft first skips the heads-up.
function promptLevel(distance, spoken) {
  if (distance <= RIDE.NEAR_PROMPT_M) return spoken < 2 ? 2 : 0;
  if (distance <= RIDE.FAR_PROMPT_M) return spoken < 1 ? 1 : 0;
  return 0;
}

// Heading for heading-up: the GPS course when moving fast enough for it to
// mean something, else the compass (null: keep the current bearing).
function pickHeading(coords, compass = null) {
  if (coords && coords.speed > RIDE.GPS_HEADING_MIN_MPS && Number.isFinite(coords.heading)) return coords.heading;
  return Number.isFinite(compass) ? compass : null;
}

// Throttle + deadband for map rotation.
function shouldRotate(currentDeg, targetDeg, msSinceLast) {
  const delta = Math.abs(((targetDeg - currentDeg + 540) % 360) - 180);
  return msSinceLast >= RIDE.BEARING_MIN_MS && delta >= RIDE.BEARING_MIN_DEG;
}

function spokenDistance(m) {
  const ft = m * FT_PER_M;
  if (ft < 1000) return `${Math.max(50, Math.round(ft / 50) * 50)} feet`;
  return `${(m / M_PER_MILE).toFixed(1)} miles`;
}

function promptText(step, level, distance) {
  const instruction = step.instruction.replace(/\.$/, "");
  if (level === 2) return `${instruction}.`;
  return `In ${spokenDistance(distance)}, ${instruction.charAt(0).toLowerCase()}${instruction.slice(1)}.`;
}

// ------------------------------------------------------- ride mode: UI
//
// "Start ride" turns the route into hands-free guidance: a banner with the
// next maneuver and a live countdown, voice prompts, auto-advance as the
// rider passes each turn, automatic reroute when off course, and a
// heading-up map that follows the rider.

const VOICE_KEY = "pev-voice-muted";
const ride = {
  active: false,
  arrived: false,
  watchId: null,
  line: null, // buildRouteLine(route.path)
  positions: [], // maneuverPositions(line, route.steps)
  next: 1, // index of the next maneuver's step
  spoken: 0, // voice prompts given for that step (see promptLevel)
  along: 0, // rider's progress along the route, meters
  offCount: 0,
  rerouting: false,
  position: null, // last rider position shown, [lon, lat]
  heading: null,
  gpsHeadingAt: -Infinity,
  bearingAt: -Infinity,
  followPausedUntil: 0,
};
let voiceMuted = storageGet(VOICE_KEY) === "1";

const riderMarker = new maplibregl.Marker({ element: makeRiderPuck(), rotationAlignment: "map" });
function makeRiderPuck() {
  const el = document.createElement("div");
  el.className = "rider-puck";
  el.setAttribute("aria-hidden", "true");
  return el;
}

// MapLibre's own location control would fight ride mode for the camera, so
// remember whether it is locked onto the user and switch it off on start.
let geolocateLocked = false;
if (geolocateControl) {
  geolocateControl.on("trackuserlocationstart", () => (geolocateLocked = true));
  geolocateControl.on("trackuserlocationend", () => (geolocateLocked = false));
}

function startRide() {
  if (!state.route || ride.active) return;
  if (!window.isSecureContext || !navigator.geolocation) {
    showTripStatus("Ride mode needs your location, which this browser can't share here. Open the app over https.", true);
    return;
  }
  ride.active = true;
  loadRideRoute(state.route);
  document.body.classList.add("riding");
  if (geolocateControl && geolocateLocked) geolocateControl.trigger(); // locked → off
  startMarker.setDraggable(false);
  endMarker.setDraggable(false);
  if (SMALL_SCREEN.matches) closeStepsDrawer(false);
  syncMuteButton();
  setRideStatus("Waiting for GPS…");
  ensureOrientation();
  acquireWakeLock();
  // Spoken from the tap itself: iOS only unlocks speech inside a gesture.
  speak(state.route.steps[0].instruction);
  startRideTracking();
}

function startRideTracking() {
  if (ride.watchId !== null) return;
  ride.watchId = navigator.geolocation.watchPosition(onRideFix, onRideError, {
    enableHighAccuracy: true,
    maximumAge: 1000,
    timeout: 20000,
  });
}

// (Re)start progress tracking on a route: on ride start and after a reroute.
function loadRideRoute(route) {
  ride.line = buildRouteLine(route.path);
  ride.positions = maneuverPositions(ride.line, route.steps);
  ride.next = 1;
  ride.spoken = 0;
  ride.along = 0;
  ride.offCount = 0;
  ride.rerouting = false;
  ride.arrived = false;
  renderRideBanner();
  markCurrentStep();
  // A new route after arriving (e.g. a vehicle change) resumes guidance.
  if (ride.active) {
    startRideTracking();
    acquireWakeLock();
  }
}

function endRide() {
  if (!ride.active) return;
  stopRideTracking();
  ride.active = false;
  ride.arrived = false;
  ride.position = null;
  riderMarker.remove();
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  releaseWakeLock();
  document.body.classList.remove("riding");
  startMarker.setDraggable(true);
  endMarker.setDraggable(true);
  markCurrentStep();
  // Back to the north-up overview of the route.
  map.setPadding({ top: 0, right: 0, bottom: 0, left: 0 });
  if (state.route) fitToPath(state.route.path, { bearing: 0, duration: REDUCED_MOTION ? 0 : 600 });
  else map.easeTo({ bearing: 0, ...FLY });
}

function stopRideTracking() {
  if (ride.watchId !== null) navigator.geolocation.clearWatch(ride.watchId);
  ride.watchId = null;
}

function onRideFix(pos) {
  if (!ride.active || ride.arrived || !ride.line) return;
  const { coords } = pos;
  const here = [coords.longitude, coords.latitude];
  const snap = snapToRoute(ride.line, here, ride.along - 50);
  const onRoute = snap.distance <= RIDE.OFF_ROUTE_M;
  if (onRoute) ride.along = snap.along;

  const gpsHeading = pickHeading(coords);
  if (gpsHeading !== null) {
    ride.heading = gpsHeading;
    ride.gpsHeadingAt = performance.now();
  }
  ride.position = onRoute ? snap.point : here;
  riderMarker.setLngLat(ride.position).setRotation(ride.heading ?? snap.bearing ?? 0).addTo(map);
  followRider();

  ride.offCount = offRouteCount(ride.offCount, snap.distance, coords.accuracy);
  if (ride.offCount >= RIDE.OFF_ROUTE_FIXES) {
    rerouteFrom(here);
    return;
  }
  if (!ride.rerouting) setRideStatus("");
  advanceRide();
}

function onRideError(err) {
  const msg =
    err.code === 1
      ? "Location permission is off — allow it for this site to ride hands-free."
      : "Waiting for a GPS fix…";
  setRideStatus(msg);
}

function advanceRide() {
  if (ride.along >= ride.line.length - RIDE.ARRIVE_M) {
    arrive();
    return;
  }
  const { index, distance } = nextManeuver(ride.positions, ride.along, ride.next);
  if (index !== ride.next) {
    ride.next = index;
    ride.spoken = 0;
    markCurrentStep();
  }
  const step = state.route.steps[index];
  const level = promptLevel(distance, ride.spoken);
  // The arrival is announced when it happens, not 100 ft out.
  if (level && !(step.maneuver === "arrive" && level === 2)) speak(promptText(step, level, distance));
  if (level) ride.spoken = level;
  renderRideBanner(distance);
}

function arrive() {
  ride.arrived = true;
  stopRideTracking();
  releaseWakeLock();
  const dest = state.route.end_label || "your destination";
  speak(`You have arrived at ${dest}.`);
  setRideStatus("");
  const banner = document.getElementById("ride-banner");
  banner.dataset.state = "arrived";
  document.getElementById("ride-arrow").dataset.maneuver = "arrive";
  document.getElementById("ride-distance").textContent = "Arrived";
  document.getElementById("ride-instruction").textContent = `You've arrived at ${dest}.`;
}

// Off route for several fixes: route again from where the rider is now.
function rerouteFrom([lon, lat]) {
  ride.offCount = 0;
  if (ride.rerouting) return;
  ride.rerouting = true;
  setRideStatus("Off route — rerouting…");
  speak("Rerouting.");
  setStart({ kind: "coords", lat, lon, label: "My location" });
}

function renderRideBanner(distance) {
  const steps = state.route.steps;
  const index = Math.min(ride.next, steps.length - 1);
  const step = steps[index];
  const d = distance ?? Math.max(0, ride.positions[index] - ride.along);
  document.getElementById("ride-banner").dataset.state = "riding";
  document.getElementById("ride-arrow").dataset.maneuver = step.maneuver;
  document.getElementById("ride-distance").textContent = fmtDist(d);
  document.getElementById("ride-instruction").textContent = step.instruction;
  const left = Math.max(0, ride.line.length - ride.along);
  const secs = state.route.distance_m ? (state.route.duration_s * left) / state.route.distance_m : 0;
  document.getElementById("ride-remaining").textContent = `${fmtDist(left)} · ${fmtMin(secs)} to go`;
}

function setRideStatus(text) {
  const el = document.getElementById("ride-status");
  el.textContent = text;
  el.classList.toggle("hidden", !text);
}

// Mark the step being approached in the overview list.
function markCurrentStep() {
  document.querySelectorAll("#steps li").forEach((li, i) => {
    const current = ride.active && !ride.arrived && i === ride.next;
    li.classList.toggle("current", current);
    if (current) li.setAttribute("aria-current", "step");
    else li.removeAttribute("aria-current");
  });
}

// Keep the rider in view, heading-up, in the lower part of the screen so
// more of the road ahead shows. Paused for a while after the rider pans.
function followRider() {
  if (!ride.position || performance.now() < ride.followPausedUntil) return;
  document.getElementById("ride-recenter").classList.add("hidden");
  const pad = mapPadding(16);
  const h = map.getContainer().clientHeight;
  pad.top += Math.max(0, Math.round((h - pad.top - pad.bottom) * 0.3));
  ride.bearingAt = performance.now();
  map.easeTo({
    center: ride.position,
    bearing: ride.heading ?? map.getBearing(),
    zoom: Math.max(map.getZoom(), 16.5),
    padding: pad,
    duration: REDUCED_MOTION ? 0 : 800,
  });
}

function rotateToHeading() {
  if (ride.heading === null || performance.now() < ride.followPausedUntil) return;
  if (!shouldRotate(map.getBearing(), ride.heading, performance.now() - ride.bearingAt)) return;
  ride.bearingAt = performance.now();
  map.rotateTo(ride.heading, { duration: REDUCED_MOTION ? 0 : 400 });
  if (ride.position) riderMarker.setRotation(ride.heading);
}

// The rider panned/zoomed by hand (or picked a step to look at): stop
// following for a while and offer a way back.
const FOLLOW_PAUSE_MS = 20000;
function pauseFollow() {
  ride.followPausedUntil = performance.now() + FOLLOW_PAUSE_MS;
  document.getElementById("ride-recenter").classList.remove("hidden");
}
map.on("movestart", (e) => {
  if (ride.active && e.originalEvent) pauseFollow();
});

function speak(text) {
  if (voiceMuted || !text || !("speechSynthesis" in window)) return;
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-US";
  speechSynthesis.speak(u);
}

function syncMuteButton() {
  const b = document.getElementById("ride-mute");
  b.setAttribute("aria-pressed", String(!voiceMuted));
  b.querySelector(".icon").textContent = voiceMuted ? "🔇" : "🔊";
}

// Publish how far an overlay reaches into the viewport from its edge (top:
// its bottom y; bottom: its height above the viewport bottom) as a CSS
// variable, so MapLibre's corner controls can step clear of it.
function exposeOverlayEdge(id, cssVar, edge) {
  const el = document.getElementById(id);
  const update = () => {
    const r = el.getBoundingClientRect();
    const px = !r.height ? 0 : edge === "top" ? r.bottom : window.innerHeight - r.top;
    document.documentElement.style.setProperty(cssVar, `${Math.ceil(px)}px`);
  };
  new ResizeObserver(update).observe(el);
  window.addEventListener("resize", update);
}
exposeOverlayEdge("ride-banner", "--ride-banner-edge", "top");
exposeOverlayEdge("ride-controls", "--ride-controls-edge", "bottom");
exposeOverlayEdge("sidebar", "--sheet-edge", "bottom"); // used by the phone layout only

document.getElementById("start-ride").addEventListener("click", startRide);
document.getElementById("end-ride").addEventListener("click", endRide);
document.getElementById("ride-steps").addEventListener("click", toggleStepsDrawer);
document.getElementById("ride-recenter").addEventListener("click", () => {
  ride.followPausedUntil = 0;
  followRider();
});
document.getElementById("ride-mute").addEventListener("click", () => {
  voiceMuted = !voiceMuted;
  storageSet(VOICE_KEY, voiceMuted ? "1" : "0");
  if (voiceMuted && "speechSynthesis" in window) speechSynthesis.cancel();
  syncMuteButton();
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
    summary.innerHTML = `<span class="warn">⚠ ${escapeHtml(err.message)}</span>`;
    if (ride.active) {
      // A failed reroute: keep guiding on the current route and try again
      // after the next few off-route fixes.
      ride.rerouting = false;
      setRideStatus(`Couldn't reroute: ${err.message}`);
      return;
    }
    state.routeShown = false;
    syncNavUI();
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
  state.route = data;
  state.routeShown = true;
  syncNavUI();
  const fc = {
    type: "Feature",
    geometry: { type: "LineString", coordinates: data.path },
  };
  map.getSource("pev-route").setData({ type: "FeatureCollection", features: [fc] });
  map.getSource("pev-route-casing").setData({ type: "FeatureCollection", features: [fc] });

  const summary = document.getElementById("route-summary");
  let html = `<b>${fmtDist(data.distance_m)}</b> · ${fmtMin(data.duration_s)} · ${data.steps.length} steps<br>`;
  html += `<span class="route-from">From ${escapeHtml(data.start_label || "start")} to ${escapeHtml(data.end_label || "destination")}</span>`;
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
  if (ride.active) {
    // A reroute mid-ride: pick up guidance on the new route, camera stays
    // with the rider.
    loadRideRoute(data);
    setRideStatus("");
    return;
  }
  // On a phone the sheet shrinks to the summary and the drawer stays shut —
  // both would cover most of the map; steps are one tap away.
  setSheetCollapsed(true);
  if (!SMALL_SCREEN.matches) openStepsDrawer(false);
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
// map (the bottom sheet on phones, the steps drawer, the ride banner and
// controls). Each overlay pads the edge it hugs, measured from the live
// layout so it is right at any viewport size (a fixed 380px left inset is
// wider than a phone).
const MAP_OVERLAYS = ["sidebar", "steps-drawer", "ride-banner", "ride-controls"];
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

  if (ride.active) pauseFollow(); // let the rider look before snapping back
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
  if (welcomeNever.checked) storageSet(WELCOME_KEY, "1");
  welcomeModal.classList.add("hidden");
}

if (!storageGet(WELCOME_KEY)) {
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
