from __future__ import annotations

import time
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import StagedRecord, TransformationException
from ..rules.engine import CompiledRuleSet, RuleError, SkipRecord, target_key, transform_record


def run_transformation(session: Session, run_id: str, rs: CompiledRuleSet, batch: int = 500) -> dict:
    t0 = time.monotonic()
    metrics = {"records": 0, "transformed": 0, "rejected": 0, "skipped": 0, "unchanged": 0, "by_rule": defaultdict(int)}
    stmt = select(StagedRecord).where(StagedRecord.run_id == run_id, StagedRecord.load_status == "STAGED").execution_options(yield_per=batch)
    exceptions = []
    for rec in session.execute(stmt).scalars():
        metrics["records"] += 1
        try:
            out, lineage = transform_record(rs, rec.table_name, rec.source_payload)
        except SkipRecord as e:
            rec.load_status = "SKIPPED"
            rec.lineage = [{"rule": e.rule_id, "field": "*", "from": "record", "to": "skipped"}]
            metrics["skipped"] += 1
            metrics["by_rule"][e.rule_id] += 1
            continue
        except RuleError as e:
            rec.load_status = "REJECTED"
            metrics["rejected"] += 1
            exceptions.append(TransformationException(run_id=run_id, stage="TRANSFORM", table_name=rec.table_name, record_key=rec.record_key, rule_id=e.rule_id, severity="ERROR", message=e.message))
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
    session.add_all(exceptions)
    session.flush()
    metrics["by_rule"] = dict(metrics["by_rule"])
    metrics["duration_s"] = round(time.monotonic() - t0, 3)
    return metrics
