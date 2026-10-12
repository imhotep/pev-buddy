"""Address search over Overture's SF address dataset."""

from __future__ import annotations

import math
import re
from difflib import get_close_matches
from functools import cached_property
from pathlib import Path

import numpy as np
import orjson

from . import config, geo

_NUM_RE = re.compile(r"(\d{1,6})[A-Z0-9]*")

REVERSE_MAX_M = 150.0  # farther than this from any address, a point has no "near" label
REVERSE_EXACT_M = 25.0  # within this, a point is labeled as the address itself
_CELL_M = 100.0  # reverse-lookup grid cell size


def display_address(number: str | None, street: str) -> str:
    """Overture's upper-case "123 VALENCIA ST" as "123 Valencia St" (ordinals
    stay "3rd", not str.title()'s "3Rd")."""
    words = [w.lower() if w[:1].isdigit() else w.title() for w in street.split()]
    return " ".join(([number] if number else []) + words)


class _Grid:
    """A uniform ~100 m grid over address points: row indices sorted by cell
    key, so the rows of a run of cells are one searchsorted slice."""

    def __init__(self, lon: np.ndarray, lat: np.ndarray):
        lat0 = float(np.mean(lat)) if len(lat) else 0.0
        self.dlat = _CELL_M / 111_320.0
        self.dlon = _CELL_M / (111_320.0 * math.cos(math.radians(lat0)))
        self.lon0 = float(lon.min()) if len(lon) else 0.0
        self.lat0 = float(lat.min()) if len(lat) else 0.0
        ix = ((lon - self.lon0) / self.dlon).astype(np.int64)
        iy = ((lat - self.lat0) / self.dlat).astype(np.int64)
        self.nx = int(ix.max()) + 1 if len(ix) else 0
        self.ny = int(iy.max()) + 1 if len(iy) else 0
        keys = ix * self.ny + iy
        self.order = np.argsort(keys, kind="stable")
        self.keys = keys[self.order]

    def cell(self, lon: float, lat: float) -> tuple[int, int]:
        return (
            math.floor((lon - self.lon0) / self.dlon),
            math.floor((lat - self.lat0) / self.dlat),
        )


def _read_rows(path: Path) -> list[tuple]:
    """Read (street, number, unit, postcode, lon, lat) rows from a parquet slice."""
    import pyarrow.parquet as pq  # sync/test path only — not a runtime dep

    table = pq.read_table(path, columns=["street", "number", "unit", "postcode", "lon", "lat"])
    return list(
        zip(
            table.column("street").to_pylist(),
            table.column("number").to_pylist(),
            table.column("unit").to_pylist(),
            table.column("postcode").to_pylist(),
            table.column("lon").to_pylist(),
            table.column("lat").to_pylist(),
        )
    )


def _vocab(values: list) -> tuple[list, np.ndarray]:
    """Dictionary-encode a column; code 0 is always None."""
    vocab: list = [None]
    index: dict = {None: 0}
    codes = np.empty(len(values), dtype=np.int32)
    for i, v in enumerate(values):
        c = index.get(v)
        if c is None:
            c = len(vocab)
            vocab.append(v)
            index[v] = c
        codes[i] = c
    return vocab, codes


def pack_addresses(rows: list[tuple]) -> tuple[dict, dict]:
    """Pack (street, number, unit, postcode, lon, lat) rows into vocab-coded
    columns grouped by street. Returns (npz_arrays, meta) — the runtime
    representation, ~15 MB for ~430k addresses vs ~110 MB of per-row dicts."""
    buckets: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        buckets.setdefault(r[0], []).append(i)
    streets = list(buckets)
    order = [i for b in buckets.values() for i in b]

    numbers, number_code = _vocab([rows[i][1] for i in order])
    units, unit_code = _vocab([rows[i][2] for i in order])
    postcodes, postcode_code = _vocab([rows[i][3] for i in order])
    offsets = np.zeros(len(streets) + 1, dtype=np.int64)
    for si, b in enumerate(buckets.values()):
        offsets[si + 1] = offsets[si] + len(b)

    arrays = {
        "number_code": number_code,
        "unit_code": unit_code,
        "postcode_code": postcode_code,
        "lon": np.array([rows[i][4] for i in order], dtype=np.float64),
        "lat": np.array([rows[i][5] for i in order], dtype=np.float64),
        "row_offsets": offsets,
    }
    meta = {"streets": streets, "numbers": numbers, "units": units, "postcodes": postcodes}
    return arrays, meta


class Geocoder:
    """Street-name address lookup over vocab-coded columns.

    Loads the compact addresses.npz + addresses_meta.json bundle written by
    sync; a parquet slice is accepted too (tests, ad-hoc extracts) and packed
    in memory the same way, so both paths behave identically.
    """

    def __init__(self, path: Path | None = None, max_results: int = 10):
        path = Path(path) if path else config.DATA_DIR / "addresses.npz"
        self.max_results = max_results
        if path.suffix == ".parquet":
            arrays, meta = pack_addresses(_read_rows(path))
        else:
            z = np.load(path)
            arrays = {k: z[k] for k in z.files}
            meta = orjson.loads(path.with_name(path.stem + "_meta.json").read_bytes())
        self.street_names: list[str] = meta["streets"]
        self.street_index: dict[str, int] = {s: i for i, s in enumerate(self.street_names)}
        self.numbers: list = meta["numbers"]
        self.units: list = meta["units"]
        self.postcodes: list = meta["postcodes"]
        self.number_of: dict[str, int] = {n: c for c, n in enumerate(self.numbers) if n is not None}
        self.row_offsets: np.ndarray = arrays["row_offsets"]
        self.number_code: np.ndarray = arrays["number_code"]
        self.unit_code: np.ndarray = arrays["unit_code"]
        self.postcode_code: np.ndarray = arrays["postcode_code"]
        self.lon: np.ndarray = arrays["lon"]
        self.lat: np.ndarray = arrays["lat"]

    def _candidates(self, street_query: str) -> list[str]:
        q = street_query.upper()
        if q in self.street_index:
            return [q]
        exact_prefix = [s for s in self.street_names if s.startswith(q)]
        if exact_prefix:
            return exact_prefix[: self.max_results * 3]
        close = get_close_matches(q, self.street_names, n=self.max_results * 3, cutoff=0.75)
        return close

    def search(self, query: str, limit: int | None = None) -> list[dict]:
        limit = limit or self.max_results
        q = query.strip()
        if not q:
            return []

        num_match = _NUM_RE.search(q)
        number = num_match.group(1) if num_match else None
        street_part = (q[: num_match.start()] + q[num_match.end() :]).strip() if num_match else q
        street_part = re.sub(r"\s+", " ", street_part).strip()
        if not street_part:
            return []

        results: list[dict] = []
        for street in self._candidates(street_part):
            si = self.street_index[street]
            lo, hi = int(self.row_offsets[si]), int(self.row_offsets[si + 1])
            if number:
                code = self.number_of.get(number)
                rows: list[int] = []
                if code is not None:
                    hits = np.nonzero(self.number_code[lo:hi] == code)[0]
                    rows = [lo + int(j) for j in hits[: max(1, limit // 3)]]
                for j in rows:
                    results.append(self._item(street, j, score=100))
            else:
                for j in range(lo, min(hi, lo + max(1, limit // 3))):
                    results.append(self._item(street, j, score=60))
            if len(results) >= limit * 2:
                break

        results.sort(key=lambda r: (-r["score"], r["street"].lower(), r["number"] or ""))
        return results[:limit]

    def reverse(self, lon: float, lat: float, max_m: float = REVERSE_MAX_M) -> dict | None:
        """The address nearest to (lon, lat), or None if none is within max_m.

        Looks only at the grid cells around the point (a few hundred
        candidates in dense SF blocks) instead of scanning all ~430k rows.
        """
        if not len(self.lon):
            return None
        grid = self._grid
        cx, cy = grid.cell(lon, lat)
        reach = int(np.ceil(max_m / _CELL_M))
        spans = []
        for x in range(cx - reach, cx + reach + 1):
            if not 0 <= x < grid.nx:
                continue
            y0, y1 = max(cy - reach, 0), min(cy + reach, grid.ny - 1)
            if y0 > y1:
                continue
            # Cells in one grid column are contiguous in key order.
            lo = np.searchsorted(grid.keys, x * grid.ny + y0, side="left")
            hi = np.searchsorted(grid.keys, x * grid.ny + y1, side="right")
            if hi > lo:
                spans.append(grid.order[lo:hi])
        if not spans:
            return None
        rows = np.concatenate(spans)
        d = geo.fast_distances(lon, lat, np.column_stack((self.lon[rows], self.lat[rows])))
        best = int(np.argmin(d))
        if d[best] > max_m:
            return None
        j = int(rows[best])
        street = self.street_names[int(np.searchsorted(self.row_offsets, j, side="right")) - 1]
        item = self._item(street, j, score=100)
        item["distance_m"] = float(d[best])
        return item

    @cached_property
    def _grid(self) -> "_Grid":
        return _Grid(self.lon, self.lat)

    def _item(self, street: str, j: int, score: int) -> dict:
        number = self.numbers[int(self.number_code[j])]
        unit = self.units[int(self.unit_code[j])]
        label = f"{number} {street}" if number else street
        if unit:
            label += f" ({unit})"
        return {
            "text": label,
            "street": street,
            "number": number,
            "unit": unit,
            "postcode": self.postcodes[int(self.postcode_code[j])],
            "lon": float(self.lon[j]),
            "lat": float(self.lat[j]),
            "score": score,
        }
