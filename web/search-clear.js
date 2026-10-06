// Issue #6: after picking Start/Destination from search, clear the query box
// and close the result list. Redefines runSearch (handlers are closures inside
// it); Escape / outside-click still keep the typed text for refining.

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
      const sub = document.createElement("small");
      sub.textContent = r.category || "Place";
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
          // Issue #6: clear the typed query after a successful Start/Destination pick.
          clearTimeout(searchTimer);
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
