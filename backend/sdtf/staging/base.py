"""Staging backends.

Extraction writes partitions of staged rows; transformation and load rewrite them with target payloads and
statuses; reconciliation and the API read them back. Two implementations share this contract:

* RelationalStaging  - rows in the metadata database (`staged_records`); simple, transactional, fine for
                       development and small scopes.
* ColumnarStaging    - one Parquet file per (run, table, partition) on any fsspec filesystem (local path, S3, GCS,
                       Azure, memory); columnar, compressible, scalable to multi-terabyte scopes and readable by
                       external tooling.

Rows are de-duplicated on (table, record_key) within a run: when several objects (or several workers) stage the
same record, the first copy wins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol


@dataclass
class StagedRow:
    partition: str
    table_name: str
    record_key: str
    source_payload: dict
    target_payload: dict | None = None
    target_key: str | None = None
    lineage: list = field(default_factory=list)
    load_status: str = "STAGED"


class StagingBackend(Protocol):
    name: str

    def write_partition(self, run_id: str, partition: str, rows: Iterable[StagedRow]) -> int: ...
    def iter_records(self, run_id: str, table: str | None = None, status: str | None = None, partition: str | None = None) -> Iterator[StagedRow]: ...
    def update_records(self, run_id: str, rows: Iterable[StagedRow]) -> int: ...
    def counts(self, run_id: str) -> list[dict]: ...
    def samples(self, run_id: str, table: str | None, limit: int) -> list[StagedRow]: ...
    def keys(self, run_id: str, table: str) -> set[str]: ...
    def contains(self, run_id: str, table: str, keys) -> set[str]: ...
    def drop_run(self, run_id: str) -> None: ...


def get_backend(name: str | None = None, session=None):
    from .. import config

    name = name or config.settings.staging_backend
    if name == "columnar":
        from .columnar import ColumnarStaging

        return ColumnarStaging(config.settings.staging_dir)
    if name == "relational":
        from .relational import RelationalStaging

        return RelationalStaging(session)
    raise ValueError(f"unknown staging backend {name!r}")
