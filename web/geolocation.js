// Issue #3: replace app.js's useMyLocation (which relied on window.alert after
// async geolocation callbacks — often suppressed) with in-page feedback.
// Loaded after app.js; the click handlers there call useMyLocation by name at
// click time, so this redefinition is what runs.

// "Use my location" feedback. Prefer the in-page #route-summary (aria-live)
// over window.alert: browsers and installed PWAs often suppress alerts from
// async geolocation callbacks, which made failures look like a silent no-op
// (issue #3).
let locating = false;

function locationErrorMessage(err) {
  if (!window.isSecureContext) {
    return "Location unavailable — this page needs HTTPS (or localhost). Pick a place instead.";
  }
  if (!navigator.geolocation) {
    return "Location unavailable in this browser — pick a place instead.";
  }
  const code = err && err.code;
  if (code === 1) {
    return "Location permission denied — enable location for this site, or pick a place.";
  }
  if (code === 2) {
    return "Location unavailable — enable location or pick a place.";
  }
  if (code === 3) {
    return "Location timed out — try again, or pick a place.";
  }
  return "Location unavailable — enable location or pick a place.";
}

function showLocationStatus(message, isError) {
  const summary = document.getElementById("route-summary");
  summary.classList.remove("hidden");
  if (isError) {
    summary.innerHTML = `<span class="warn">${escapeHtml(message)}</span>`;
  } else {
    summary.textContent = message;
  }
}

function setLocationButtonsBusy(busy) {
  for (const id of ["use-location", "use-location-end"]) {
    const btn = document.getElementById(id);
    btn.disabled = busy;
    if (busy) {
      if (!btn.dataset.label) btn.dataset.label = btn.textContent;
      btn.textContent = "Locating…";
      btn.setAttribute("aria-busy", "true");
    } else {
      btn.textContent = btn.dataset.label || "Use my location";
      btn.removeAttribute("aria-busy");
    }
  }
}

async function useMyLocation(which) {
  if (locating) return;
  if (!window.isSecureContext || !navigator.geolocation) {
    showLocationStatus(locationErrorMessage(), true);
    return;
  }
  // If permission is already denied, say so immediately instead of waiting
  // for getCurrentPosition to fail (or hang) with no UI change.
  try {
    if (navigator.permissions && navigator.permissions.query) {
      const status = await navigator.permissions.query({ name: "geolocation" });
      if (status.state === "denied") {
        showLocationStatus(locationErrorMessage({ code: 1 }), true);
        return;
      }
    }
  } catch (_) {
    // Safari and some WebViews throw on geolocation permission queries.
  }

  locating = true;
  setLocationButtonsBusy(true);
  showLocationStatus("Getting your location…", false);

  navigator.geolocation.getCurrentPosition(
    (pos) => {
      locating = false;
      setLocationButtonsBusy(false);
      // Clear the transient locating line; computeRoute will rewrite it if
      // both ends are now set.
      const summary = document.getElementById("route-summary");
      summary.classList.add("hidden");
      summary.textContent = "";
      const ref = {
        kind: "coords",
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        label: "My location",
      };
      if (which === "end") setEnd(ref);
      else setStart(ref);
      map.flyTo({ center: [pos.coords.longitude, pos.coords.latitude], zoom: 14, ...FLY });
    },
    (err) => {
      locating = false;
      setLocationButtonsBusy(false);
      showLocationStatus(locationErrorMessage(err), true);
    },
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 }
  );
}
