from __future__ import annotations

import time
from collections import defaultdict

from sqlalchemy.orm import Session

from ..models import TransformationException
from ..rules.engine import CompiledRuleSet, RuleError, SkipRecord, target_key, transform_record
from ..staging import get_backend


def run_transformation(session: Session, run_id: str, rs: CompiledRuleSet, batch: int = 500, backend=None, partition: str | None = None) -> dict:
    backend = backend or get_backend(session=session)
    t0 = time.monotonic()
    metrics = {"records": 0, "transformed": 0, "rejected": 0, "skipped": 0, "unchanged": 0, "by_rule": defaultdict(int)}
    exceptions = []
    buf = []
    for rec in backend.iter_records(run_id, status="STAGED", partition=partition):
        metrics["records"] += 1
        try:
            out, lineage = transform_record(rs, rec.table_name, rec.source_payload)
        except SkipRecord as e:
            rec.load_status = "SKIPPED"
            rec.lineage = [{"rule": e.rule_id, "field": "*", "from": "record", "to": "skipped"}]
            metrics["skipped"] += 1
            metrics["by_rule"][e.rule_id] += 1
            buf.append(rec)
            continue
        except RuleError as e:
            rec.load_status = "REJECTED"
            metrics["rejected"] += 1
            exceptions.append(TransformationException(run_id=run_id, stage="TRANSFORM", table_name=rec.table_name, record_key=rec.record_key, rule_id=e.rule_id, severity="ERROR", message=e.message))
            buf.append(rec)
            continue
        rec.target_payload = out
        rec.target_key = target_key(rec.table_name, out)
        rec.lineage = lineage
        rec.load_status = "TRANSFORMED"
        if lineage:
            metrics["transformed"] += 1
            for l in lineage:
                metrics["by_rule"][l["rule"]] += 1
        else:
            metrics["unchanged"] += 1
        buf.append(rec)
    backend.update_records(run_id, buf)
    session.add_all(exceptions)
    session.flush()
    metrics["by_rule"] = dict(metrics["by_rule"])
    metrics["duration_s"] = round(time.monotonic() - t0, 3)
    return metrics
