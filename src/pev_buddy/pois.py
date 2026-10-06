"""Named-POI search over Overture's SF places dataset (businesses, parks, ...)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import orjson

from . import config
from .geo import haversine

# When several query hits form a tight geographic cluster, drop places that
# sit far outside it. Catches bad Overture/OSM rows that reuse a well-known
# name with coords elsewhere in the city (issue #1: "Ferry Building,
# Embarcadero" Historic Site pinned in Bernal Heights ~7 km from the real
# Embarcadero cluster). Broad one-word queries that legitimately span SF
# (e.g. "Mission") rarely form a dense cluster, so they are left alone.
DENSE_CLUSTER_M = 800.0
OUTLIER_M = 3000.0
MIN_DENSE_CLUSTER = 3


def format_category(cat: str | None) -> str:
    if not cat:
        return "Place"
    return cat.replace("_", " ").title()


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def suppress_coordinate_outliers(
    results: list[dict],
    *,
    dense_m: float = DENSE_CLUSTER_M,
    outlier_m: float = OUTLIER_M,
    min_dense: int = MIN_DENSE_CLUSTER,
) -> list[dict]:
    """Drop places far from a dense peer cluster in the same result set.

    Uses a robust median of candidate coords; if at least `min_dense` fall
    within `dense_m` of that median, treat them as the trusted cluster and
    drop any candidate farther than `outlier_m` from the dense-cluster median.
    """
    if len(results) < min_dense + 1:
        return results

    med_lat = _median([r["lat"] for r in results])
    med_lon = _median([r["lon"] for r in results])
    dense = [r for r in results if haversine(med_lon, med_lat, r["lon"], r["lat"]) <= dense_m]
    if len(dense) < min_dense:
        return results

    cluster_lat = _median([r["lat"] for r in dense])
    cluster_lon = _median([r["lon"] for r in dense])
    kept = [
        r for r in results if haversine(cluster_lon, cluster_lat, r["lon"], r["lat"]) <= outlier_m
    ]
    return kept if kept else results


class POIIndex:
    """Case-insensitive name search over a compact [id, name, category, address, lon, lat] list.

    Exact > prefix > substring. Deliberately no fuzzy pass: with ~80k names it
    would dominate latency, and typed POI queries are overwhelmingly covered by
    the three tiers above.

    Rows are stored as parallel vocab-coded columns (a dict per place would
    roughly triple the memory for no behavior gain); the place id is dropped —
    nothing consumes it.
    """

    def __init__(self, path: Path | None = None):
        path = path or config.DATA_DIR / "places.json"
        rows = orjson.loads(Path(path).read_bytes())
        n = len(rows)
        self.names: list[str] = [r[1] for r in rows]
        self.addresses: list = [r[3] for r in rows]
        self.lon = np.array([r[4] for r in rows], dtype=np.float64)
        self.lat = np.array([r[5] for r in rows], dtype=np.float64)
        cat_vocab: list = [None]
        cat_index: dict = {None: 0}
        cat_code = np.empty(n, dtype=np.int32)
        grouped: dict[str, list[int]] = {}
        for i, r in enumerate(rows):
            c = cat_index.get(r[2])
            if c is None:
                c = len(cat_vocab)
                cat_vocab.append(r[2])
                cat_index[r[2]] = c
            cat_code[i] = c
            grouped.setdefault(r[1].casefold(), []).append(i)
        self.cat_vocab = cat_vocab
        self.cat_code = cat_code
        self.by_name: dict[str, tuple[int, ...]] = {k: tuple(v) for k, v in grouped.items()}

    def __len__(self) -> int:
        return len(self.names)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        q = (query or "").strip().casefold()
        if not q:
            return []
        scored: list[tuple[int, int]] = []
        for i in self.by_name.get(q, ()):
            scored.append((100, i))
        if len(scored) < limit * 2:
            for name, idxs in self.by_name.items():
                if name == q:
                    continue  # already scored as exact
                if name.startswith(q):
                    for i in idxs:
                        scored.append((80, i))
                elif q in name:
                    for i in idxs:
                        scored.append((60, i))
        scored.sort(key=lambda t: (-t[0], len(self.names[t[1]]), self.names[t[1]].casefold()))
        # Over-fetch before outlier suppression so a dense peer cluster can form
        # even when a bad row ranks near the top of the truncated window.
        pool = max(limit * 3, 24)
        out: list[dict] = []
        for score, i in scored[:pool]:
            out.append(
                {
                    "name": self.names[i],
                    "category": format_category(self.cat_vocab[int(self.cat_code[i])]),
                    "address": self.addresses[i],
                    "lon": float(self.lon[i]),
                    "lat": float(self.lat[i]),
                    "score": score,
                }
            )
        return suppress_coordinate_outliers(out)[:limit]
