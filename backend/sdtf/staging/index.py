"""Per-file key-range index for columnar staging.

Each Parquet partition file `<partition>.parquet` has a sidecar `<partition>.idx.json` with the record count,
min/max record key, load-status counts and a Bloom filter over the keys. Membership checks (write-time
de-duplication, `contains`) prune files by range and Bloom filter before reading any Parquet column; `counts()`
is answered from sidecars alone. Files without a sidecar (older writers) are scanned.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Iterable

INDEX_SUFFIX = ".idx.json"
INDEX_VERSION = 1


class Bloom:
    """Small, dependency-free Bloom filter (blake2b with per-hash salts)."""

    def __init__(self, m: int, k: int, bits: bytearray | None = None):
        self.m, self.k = m, k
        self.bits = bits if bits is not None else bytearray((m + 7) // 8)

    @classmethod
    def for_items(cls, n: int, fpr: float = 0.01) -> Bloom:
        n = max(1, n)
        m = max(64, int(-n * math.log(fpr) / (math.log(2) ** 2)))
        k = max(1, min(12, round(m / n * math.log(2))))
        return cls(m, k)

    def _positions(self, key: str):
        for i in range(self.k):
            h = hashlib.blake2b(key.encode(), digest_size=8, salt=i.to_bytes(2, "little")).digest()
            yield int.from_bytes(h, "little") % self.m

    def add(self, key: str) -> None:
        for p in self._positions(key):
            self.bits[p >> 3] |= 1 << (p & 7)

    def maybe_contains(self, key: str) -> bool:
        return all(self.bits[p >> 3] & (1 << (p & 7)) for p in self._positions(key))

    def to_json(self) -> dict:
        return {"m": self.m, "k": self.k, "bits": base64.b64encode(bytes(self.bits)).decode()}

    @classmethod
    def from_json(cls, d: dict) -> Bloom:
        return cls(d["m"], d["k"], bytearray(base64.b64decode(d["bits"])))


@dataclass
class FileIndex:
    count: int
    min_key: str
    max_key: str
    status_counts: dict[str, int]
    bloom: Bloom
    version: int = INDEX_VERSION
    extra: dict = field(default_factory=dict)

    @classmethod
    def build(cls, keys: Iterable[str], statuses: Iterable[str]) -> FileIndex:
        keys = list(keys)
        bloom = Bloom.for_items(len(keys))
        for k in keys:
            bloom.add(k)
        sc: dict[str, int] = {}
        for s in statuses:
            sc[s] = sc.get(s, 0) + 1
        return cls(len(keys), min(keys) if keys else "", max(keys) if keys else "", sc, bloom)

    def may_contain(self, key: str) -> bool:
        if self.count == 0 or key < self.min_key or key > self.max_key:
            return False
        return self.bloom.maybe_contains(key)

    def overlaps(self, lo: str, hi: str) -> bool:
        return self.count > 0 and not (hi < self.min_key or lo > self.max_key)

    def to_json(self) -> str:
        return json.dumps({"version": self.version, "count": self.count, "min_key": self.min_key, "max_key": self.max_key, "status_counts": self.status_counts, "bloom": self.bloom.to_json(), **self.extra}, separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> FileIndex:
        d = json.loads(s)
        return cls(d["count"], d["min_key"], d["max_key"], d.get("status_counts", {}), Bloom.from_json(d["bloom"]), d.get("version", 1))


def index_path(parquet_path: str) -> str:
    return parquet_path[: -len(".parquet")] + INDEX_SUFFIX
