"""Scale smoke: the slice must stay linear-ish and complete at scale 3 (≈33k rows). Records timings for docs/benchmarks.md."""
import json
import os
import time

from sdtf.demo import run_vertical_slice


def test_scale_three_completes(engine):
    from sdtf.db import session_scope

    t0 = time.monotonic()
    with session_scope() as s:
        out = run_vertical_slice(s, scale=3, seed=5)
        run = out["run"]
        total = time.monotonic() - t0
        assert run.status == "COMPLETED" and run.report["reconciliation"]["overall"] == "PASS"
        stages = {st.name: st for st in run.stages}
        timing = {"scale": 3, "graph_nodes": out["graph_stats"]["nodes"], "graph_edges": out["graph_stats"]["edges"], "objects_in_scope": out["manifest"].impact["objects_total"], "extract_records": stages["EXTRACT"].metrics["records"], "extract_rps": stages["EXTRACT"].metrics["records_per_second"], "stage_ms": {k: v.duration_ms for k, v in stages.items()}, "total_s": round(total, 2)}
        path = os.path.join(os.environ.get("SDTF_EVIDENCE_DIR", "."), "scale_timing.json")
        with open(path, "w") as fh:
            json.dump(timing, fh, indent=2)
        assert total < 120, timing
