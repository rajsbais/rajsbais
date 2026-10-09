from __future__ import annotations

from typing import Iterable, Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import StagedRecord
from .base import StagedRow


def _row(r: StagedRecord) -> StagedRow:
    return StagedRow(r.partition, r.table_name, r.record_key, r.source_payload, r.target_payload, r.target_key, r.lineage or [], r.load_status)


class RelationalStaging:
    name = "relational"

    def __init__(self, session: Session):
        if session is None:
            raise ValueError("RelationalStaging needs a session")
        self.session = session

    def write_partition(self, run_id: str, partition: str, rows: Iterable[StagedRow]) -> int:
        buf = [{"run_id": run_id, "partition": partition, "table_name": r.table_name, "record_key": r.record_key, "source_payload": r.source_payload, "target_payload": r.target_payload, "target_key": r.target_key, "lineage": r.lineage, "load_status": r.load_status} for r in rows]
        if not buf:
            return 0
        dialect = self.session.get_bind().dialect.name
        n = 0
        for i in range(0, len(buf), 1000):
            chunk = buf[i : i + 1000]
            if dialect == "sqlite":
                stmt = StagedRecord.__table__.insert().prefix_with("OR IGNORE")
            elif dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                stmt = pg_insert(StagedRecord.__table__).on_conflict_do_nothing(constraint="uq_staged")
            else:  # pragma: no cover
                stmt = StagedRecord.__table__.insert()
            res = self.session.execute(stmt, chunk)
            n += res.rowcount if res.rowcount is not None and res.rowcount >= 0 else len(chunk)
        self.session.flush()
        return n

    def iter_records(self, run_id: str, table: str | None = None, status: str | None = None, partition: str | None = None) -> Iterator[StagedRow]:
        stmt = select(StagedRecord).where(StagedRecord.run_id == run_id)
        if table:
            stmt = stmt.where(StagedRecord.table_name == table)
        if partition:
            stmt = stmt.where(StagedRecord.partition == partition)
        if status:
            stmt = stmt.where(StagedRecord.load_status == status)
        for r in self.session.execute(stmt.order_by(StagedRecord.id).execution_options(yield_per=1000, populate_existing=True)).scalars():
            yield _row(r)

    def update_records(self, run_id: str, rows: Iterable[StagedRow]) -> int:
        n = 0
        for i, r in enumerate(rows):
            self.session.execute(StagedRecord.__table__.update().where(StagedRecord.run_id == run_id, StagedRecord.table_name == r.table_name, StagedRecord.record_key == r.record_key).values(target_payload=r.target_payload, target_key=r.target_key, lineage=r.lineage, load_status=r.load_status))
            n += 1
            if i % 1000 == 999:
                self.session.flush()
        self.session.flush()
        self.session.expire_all()  # Core updates bypass the identity map
        return n

    def counts(self, run_id: str) -> list[dict]:
        rows = self.session.execute(select(StagedRecord.table_name, StagedRecord.load_status, func.count()).where(StagedRecord.run_id == run_id).group_by(StagedRecord.table_name, StagedRecord.load_status)).all()
        return [{"table": t, "status": s, "count": n} for t, s, n in rows]

    def samples(self, run_id: str, table: str | None, limit: int) -> list[StagedRow]:
        stmt = select(StagedRecord).where(StagedRecord.run_id == run_id)
        if table:
            stmt = stmt.where(StagedRecord.table_name == table)
        return [_row(r) for r in self.session.execute(stmt.limit(limit).execution_options(populate_existing=True)).scalars()]

    def keys(self, run_id: str, table: str) -> set[str]:
        return {k for (k,) in self.session.execute(select(StagedRecord.record_key).where(StagedRecord.run_id == run_id, StagedRecord.table_name == table))}

    def drop_run(self, run_id: str) -> None:
        self.session.query(StagedRecord).filter(StagedRecord.run_id == run_id).delete()
