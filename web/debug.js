// PEV Buddy developer tools, for the browser console (on a phone: Safari
// Web Inspector). Nothing here runs until called; `pev.help()` lists it all.
//
//   pev.hud()            toggle an on-screen readout: viewport/scroll, map
//                        gesture handling, the last touch gesture, recent
//                        camera calls and console warnings. Remembered
//                        across reloads (localStorage), so it also works in
//                        the installed app, which has no URL bar.
//   pev.route()          the current route: summary plus a steps table
//   pev.fix(lat, lon)    feed ride mode one GPS fix (as if the phone moved)
//   pev.drive(mph)       ride the current route with simulated fixes;
//                        pev.stop() ends it
//   pev.state()          snapshot of app, ride and camera state
//   pev.resetApp()       drop the service worker + caches and reload (when
//                        a phone is stuck on an old build)
//
// Adding `?debug` to the URL turns the HUD on as well.
(() => {
  const HUD_KEY = "pev-debug-hud";
  const store = {
    get: () => {
      try {
        return localStorage.getItem(HUD_KEY) === "1";
      } catch {
        return false;
      }
    },
    set: (on) => {
      try {
        if (on) localStorage.setItem(HUD_KEY, "1");
        else localStorage.removeItem(HUD_KEY);
      } catch {
        /* private mode: the HUD just won't be remembered */
      }
    },
  };

  const desc = (el) =>
    !el
      ? "null"
      : el === document
        ? "document"
        : el.tagName.toLowerCase() +
          (el.id ? "#" + el.id : "") +
          (typeof el.className === "string" && el.className ? "." + el.className.trim().split(/\s+/)[0] : "");

  // ------------------------------------------------------------------ HUD

  let hud = null; // { el, timer, undo: [] }

  function startHud() {
    if (hud) return;
    const el = document.createElement("pre");
    el.id = "pev-debug-hud";
    el.style.cssText =
      "position:fixed;left:4px;top:calc(env(safe-area-inset-top) + 4px);z-index:99999;margin:0;padding:6px;" +
      "background:rgba(0,0,0,.85);color:#7f7;font:10px/1.3 monospace;pointer-events:none;max-width:75vw;white-space:pre-wrap";
    document.body.appendChild(el);
    const undo = [];
    const listen = (target, type, fn, opts) => {
      target.addEventListener(type, fn, opts);
      undo.push(() => target.removeEventListener(type, fn, opts));
    };

    // The last one-finger gesture: where it started, whether the map took
    // it (touchmove defaultPrevented), whether iOS cancelled it, what
    // scrolled instead, and how far the map moved.
    const g = { start: "-", moves: 0, prevented: 0, cancel: "no", scrolled: new Set(), mapMoved: "no", end: "-", c0: null };
    const opts = { capture: true, passive: true };
    listen(document, "touchstart", (e) => {
      if (e.touches.length === 1) {
        Object.assign(g, { moves: 0, prevented: 0, cancel: "no", scrolled: new Set(), mapMoved: "no", end: "-" });
        g.c0 = map.getCenter();
      }
      const t = e.touches[0];
      g.start = `${e.touches.length}t on ${desc(e.target)} @${Math.round(t.clientX)},${Math.round(t.clientY)}`;
    }, opts);
    listen(document, "touchmove", (e) => {
      g.moves++;
      setTimeout(() => e.defaultPrevented && g.prevented++);
    }, opts);
    listen(document, "touchend", () => {
      if (!g.c0) return;
      const c = map.getCenter();
      g.end = `dx=${Math.round((c.lng - g.c0.lng) * 88000)}m dy=${Math.round((c.lat - g.c0.lat) * 111000)}m`;
    }, opts);
    listen(document, "touchcancel", () => (g.cancel = "YES (iOS took it)"), opts);
    listen(document, "scroll", (e) => g.scrolled.add(desc(e.target)), opts);
    if (window.visualViewport) listen(visualViewport, "scroll", () => g.scrolled.add("visualViewport"));
    const onMove = (e) => (g.mapMoved = e.originalEvent ? "by finger" : "by code");
    map.on("movestart", onMove);
    undo.push(() => map.off("movestart", onMove));

    // Recent camera calls, with who made them.
    const calls = [];
    for (const k of ["easeTo", "jumpTo", "flyTo", "fitBounds", "setPadding", "rotateTo", "stop"]) {
      const orig = map[k];
      map[k] = function (...a) {
        const who = (new Error().stack || "")
          .split("\n")
          .slice(2, 4)
          .map((l) => (l.match(/at (\w+)/) || l.match(/^(\w+)@/) || [])[1] || "?")
          .join("<");
        calls.push(`${k} ${who}`);
        if (calls.length > 5) calls.shift();
        return orig.apply(this, a);
      };
      undo.push(() => (map[k] = orig));
    }

    // The last console warning or error (MapLibre reports refused camera
    // moves that way, e.g. "Map cannot fit within canvas").
    let lastWarn = "-";
    for (const k of ["warn", "error"]) {
      const orig = console[k];
      console[k] = (...a) => {
        lastWarn = `${k}: ${a.map(String).join(" ").slice(0, 90)}`;
        orig.apply(console, a);
      };
      undo.push(() => (console[k] = orig));
    }
    listen(window, "error", (ev) => (lastWarn = "ERR " + ev.message));

    const paint = () => {
      const vv = window.visualViewport;
      let active = "?";
      try {
        active = map.handlers._handlers.filter((h) => h.handler.isActive()).map((h) => h.handlerName).join(",") || "none";
      } catch {
        /* MapLibre internals moved */
      }
      const p = map.getPadding();
      const z = map.getZoom().toFixed(1);
      el.textContent = [
        `win ${innerWidth}x${innerHeight} vv ${vv ? `${Math.round(vv.width)}x${Math.round(vv.height)} s${vv.scale.toFixed(2)}` : "?"} scroll ${scrollY}`,
        `map ${map.getContainer().clientWidth}x${map.getContainer().clientHeight} z${z} pad ${Math.round(p.top)}/${Math.round(p.bottom)} dragPan ${map.dragPan.isEnabled()} active ${active}`,
        `body.${document.body.className || "-"} route ${!!state.route} ride ${ride.active}${ride.active ? ` next ${ride.next} off ${ride.offCount}` : ""}`,
        `gesture ${g.start}`,
        `  moves ${g.moves} prevented ${g.prevented} cancel ${g.cancel} scrolled ${[...g.scrolled].join(",") || "nothing"}`,
        `  map ${g.mapMoved} ${g.end}`,
        `warn ${lastWarn}`,
        ...calls.map((c) => "  " + c),
      ].join("\n");
    };
    hud = { el, timer: setInterval(paint, 300), undo };
    paint();
  }

  function stopHud() {
    if (!hud) return;
    clearInterval(hud.timer);
    hud.undo.forEach((f) => f());
    hud.el.remove();
    hud = null;
  }

  // ------------------------------------------------------------ GPS sim

  let driving = null;

  function fix(lat, lon, { accuracy = 8, heading = null, speed = null } = {}) {
    onRideFix({ coords: { latitude: lat, longitude: lon, accuracy, heading, speed }, timestamp: Date.now() });
  }

  function bearing([lon0, lat0], [lon1, lat1]) {
    const r = Math.PI / 180;
    const y = Math.sin((lon1 - lon0) * r) * Math.cos(lat1 * r);
    const x = Math.cos(lat0 * r) * Math.sin(lat1 * r) - Math.sin(lat0 * r) * Math.cos(lat1 * r) * Math.cos((lon1 - lon0) * r);
    return (Math.atan2(y, x) / r + 360) % 360;
  }

  function drive(mph = 12) {
    if (!state.route) return "No route: plan one first.";
    if (!ride.active) startRide();
    stop();
    const path = state.route.path;
    const mps = mph * 0.44704;
    let i = 0;
    let t = 0; // 0..1 along path[i] -> path[i + 1]
    driving = setInterval(() => {
      let budget = mps; // meters this second
      while (i < path.length - 1 && budget > 0) {
        const a = path[i];
        const b = path[i + 1];
        const len = Math.hypot((b[0] - a[0]) * 88000, (b[1] - a[1]) * 111000) || 1e-6;
        const left = (1 - t) * len;
        if (budget >= left) {
          budget -= left;
          i++;
          t = 0;
        } else {
          t += budget / len;
          budget = 0;
        }
      }
      if (i >= path.length - 1) {
        const end = path[path.length - 1];
        fix(end[1], end[0]);
        stop();
        return;
      }
      const a = path[i];
      const b = path[i + 1];
      fix(a[1] + (b[1] - a[1]) * t, a[0] + (b[0] - a[0]) * t, { heading: bearing(a, b), speed: mps });
    }, 1000);
    return `Driving at ${mph} mph; pev.stop() to end.`;
  }

  function stop() {
    if (driving) clearInterval(driving);
    driving = null;
  }

  // ------------------------------------------------------------- the rest

  function route() {
    const r = state.route;
    if (!r) return "No route.";
    console.table(
      r.steps.map((s) => ({ step: s.index, instruction: s.instruction, ft: Math.round((s.distance_m || 0) * 3.28084), s: s.duration_s }))
    );
    return {
      miles: +(r.distance_m / 1609.344).toFixed(2),
      minutes: Math.round(r.duration_s / 60),
      climb_ft: Math.round((r.climb_m || 0) * 3.28084),
      descent_ft: Math.round((r.descent_m || 0) * 3.28084),
      vehicle: r.vehicle,
      from: r.start_label,
      to: r.end_label,
      segment_stats: r.segment_stats,
      warnings: r.warnings,
    };
  }

  function snapshot() {
    const c = map.getCenter();
    return {
      start: state.startRef,
      end: state.endRef,
      routeShown: state.routeShown,
      activeStep: state.activeStep,
      ride: { ...ride, line: ride.line ? `${ride.line.length} pts` : null, positions: undefined },
      camera: { center: [+c.lng.toFixed(5), +c.lat.toFixed(5)], zoom: +map.getZoom().toFixed(2), bearing: Math.round(map.getBearing()), padding: map.getPadding() },
      build: document.querySelector('script[src*="app.js"]')?.getAttribute("src"),
    };
  }

  async function resetApp() {
    const regs = (await navigator.serviceWorker?.getRegistrations()) || [];
    await Promise.all(regs.map((r) => r.unregister()));
    const keys = (await caches?.keys()) || [];
    await Promise.all(keys.map((k) => caches.delete(k)));
    location.reload();
  }

  function help() {
    console.log(
      [
        "pev.hud()           toggle the on-screen debug readout (remembered)",
        "pev.route()         current route summary + steps table",
        "pev.fix(lat, lon)   feed ride mode one GPS fix",
        "pev.drive(mph=12)   ride the current route with simulated GPS; pev.stop() ends it",
        "pev.state()         app / ride / camera snapshot",
        "pev.resetApp()      drop service worker + caches, then reload",
      ].join("\n")
    );
  }

  window.pev = {
    help,
    hud(on) {
      const want = on === undefined ? !hud : !!on;
      store.set(want);
      if (want) startHud();
      else stopHud();
      return want ? "HUD on" : "HUD off";
    },
    route,
    fix,
    drive,
    stop,
    state: snapshot,
    resetApp,
  };

  if (store.get() || new URLSearchParams(location.search).has("debug")) startHud();
})();
