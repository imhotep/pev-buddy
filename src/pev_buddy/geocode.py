"""Address search over Overture's SF address dataset."""

from __future__ import annotations

import re
from difflib import get_close_matches
from pathlib import Path

import numpy as np
import orjson

from . import config

_NUM_RE = re.compile(r"(\d{1,6})[A-Z0-9]*")


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
