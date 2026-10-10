"""Benchmark harness: the measurement plumbing of docs/benchmarks.md. Runs the vertical slice at a given scale on
the synthetic landscape through the simulated add-on and gateway, times every step from the generator to the
reconciliation, and writes a Markdown report that names the environment. Every number is a simulated-run figure
and the report says so; the protocol for real measurements stays in docs/benchmarks.md."""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .catalog.store import RecordStore
from .demo import create_demo_project, demo_scope_definition
from .discovery.rfc_discovery import discover_over_rfc
from .discovery.service import discover_system
from .graph.service import build_graph, persist_graph
from .models import ReconciliationResult
from .scope.service import create_manifest, evaluate_scope


def _t() -> float:
    return time.perf_counter()


def environment() -> dict:
    return {"python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(), "cpus": os.cpu_count(), "database": os.getenv("SDTF_DATABASE_URL", "sqlite (default)").split("://")[0], "staging": os.getenv("SDTF_STAGING_BACKEND", "relational"), "measured_at": datetime.now(UTC).isoformat()}


def run_benchmark(session: Session, scale: int = 1, seed: int = 42, workers: int = 4, connector: str = "RFC", load_mode: str = "api", with_rfc_discovery: bool = True) -> dict:
    """One measurement at `scale`: generation and import, discovery (record store and through the add-on), graph,
    scope evaluation, the run's stages, reconciliation. Returns the figures; nothing is persisted but the run."""
    from .demo import apply_disposition, approve_manifest, approve_ruleset, pending_dispositions
    from .models import RuleSet
    from .rules.engine import parse_ruleset, validate_ruleset
    from .rules.factory import generate_candidate_ruleset
    from .runtime.pipeline import start_run

    out: dict = {"scale": scale, "seed": seed, "workers": workers, "connector": connector, "load_mode": load_mode, "simulated": True, "steps": {}}
    t0 = _t()
    ctx = create_demo_project(session, "benchmark", scale=scale, seed=seed, name=f"Benchmark scale {scale}", connector=connector)
    src, tgt, project = ctx["source"], ctx["target"], ctx["project"]
    store = RecordStore.load(session, src.id)
    rows = sum(len(store.rows(t)) for t in store.tables())
    out["landscape"] = {"tables": len(store.tables()), "rows": rows}
    out["steps"]["generate_and_import_s"] = round(_t() - t0, 3)
    if with_rfc_discovery:
        t = _t()
        snap2 = discover_over_rfc(session, src, "benchmark")
        out["steps"]["discovery_add_on_s"] = round(_t() - t, 3)
        out["discovery_add_on"] = {"rfc_calls": snap2.summary["read"].get("rfc_calls"), "rows_read": snap2.summary["read"].get("rows_read"), "tables_sized": snap2.summary["read"].get("tables_sized"), "complete": snap2.summary["read"].get("complete")}
    t = _t()
    snap = discover_system(session, src, "benchmark", store)
    out["steps"]["discovery_record_store_s"] = round(_t() - t, 3)
    out["landscape"]["business_objects"] = sum(i["count"] for i in snap.summary["business_objects"].values())
    t = _t()
    g = build_graph(store, src.id)
    gstats = persist_graph(session, src.id, g)
    out["steps"]["graph_s"] = round(_t() - t, 3)
    out["graph"] = {"nodes": gstats.get("nodes"), "edges": gstats.get("edges")}
    defn = demo_scope_definition(src, tgt)
    t = _t()
    ev = evaluate_scope(session, defn)
    out["steps"]["scope_evaluate_s"] = round(_t() - t, 3)
    out["scope"] = {"objects": ev["impact"]["objects_total"], "approvals_required": ev["impact"]["approvals_required"]}
    manifest = create_manifest(session, project.id, defn, "benchmark")
    pending = pending_dispositions(manifest)
    apply_disposition(session, manifest, pending, "TRANSFER", "approver", "benchmark")
    approve_manifest(session, manifest, "approver")
    yaml_src = generate_candidate_ruleset(defn, "S4HANA")
    rs = parse_ruleset(yaml_src)
    ruleset = RuleSet(project_id=project.id, name=rs.name, version=1, content_hash=rs.content_hash, source_yaml=yaml_src, compiled={"rules": rs.rules, "lookups": rs.lookups}, validation=validate_ruleset(rs), status="DRAFT", created_by="benchmark")
    session.add(ruleset)
    session.flush()
    approve_ruleset(session, ruleset, "approver")
    t = _t()
    run = start_run(session, project.id, manifest.id, ruleset.id, "benchmark", "SIMULATED", workers, load_mode=load_mode)
    out["steps"]["run_s"] = round(_t() - t, 3)
    out["run"] = {"id": run.id, "status": run.status, "stages": {s.name: {"ms": s.duration_ms, **{k: v for k, v in (s.metrics or {}).items() if isinstance(v, (int, float)) and k in ("records", "rows", "records_per_second", "partitions", "checks", "loaded", "applied")}} for s in run.stages}}
    ext = next((s for s in run.stages if s.name == "EXTRACT"), None)
    out["extraction"] = {"records": (ext.metrics or {}).get("records") or (ext.metrics or {}).get("rows"), "records_per_second": (ext.metrics or {}).get("records_per_second")} if ext else {}
    checks = session.query(ReconciliationResult).filter(ReconciliationResult.run_id == run.id).count()
    out["reconciliation"] = {"checks": checks, "overall": (run.report or {}).get("reconciliation", {}).get("overall")}
    out["steps"]["end_to_end_s"] = round(_t() - t0, 3)
    return out


def report_markdown(results: list[dict], env: dict | None = None) -> str:
    env = env or environment()
    md = ["## Measured on this environment (simulated run, no SAP system)", "", f"Python {env['python']} · {env['platform']} · {env['cpus']} CPUs · database {env['database']} · staging {env['staging']} · measured {env['measured_at']}", "", "No throughput or downtime guarantee is derived from these figures: the source and the target are the platform's simulators on the synthetic landscape; they validate the measurement plumbing and give the order of magnitude of the engine itself.", "", "| Scale | Rows | Objects | Generate + import | Discovery (store / add-on) | Graph | Scope | Extract (rec/s) | Transform | Load | Reconcile (checks) | End to end |", "|---:|---:|---:|---:|---|---:|---:|---|---:|---:|---|---:|"]
    for r in results:
        st, stages = r["steps"], r["run"]["stages"]

        def ms(n: str, stages: dict = stages) -> str:
            return f"{stages.get(n, {}).get('ms', 0) / 1000:.2f} s"

        rps = r["extraction"].get("records_per_second")
        md.append(f"| {r['scale']} | {r['landscape']['rows']:,} | {r['landscape']['business_objects']:,} | {st['generate_and_import_s']:.2f} s | {st['discovery_record_store_s']:.2f} s / {st.get('discovery_add_on_s', 0):.2f} s | {st['graph_s']:.2f} s | {st['scope_evaluate_s']:.2f} s | {ms('EXTRACT')}{f' ({rps:,.0f})' if rps else ''} | {ms('TRANSFORM')} | {ms('LOAD')} | {ms('RECONCILE')} ({r['reconciliation']['checks']}) | {st['end_to_end_s']:.2f} s |")
    md.append("")
    return "\n".join(md)


def write_report(results: list[dict], path: str, env: dict | None = None) -> None:
    """Replace the measured section of docs/benchmarks.md (between the markers) or append it."""
    start, end = "<!-- benchmark:measured:start -->", "<!-- benchmark:measured:end -->"
    block = f"{start}\n{report_markdown(results, env)}{end}\n"
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    if start in text and end in text:
        text = text[: text.index(start)] + block + text[text.index(end) + len(end) + 1 :]
    else:
        text = text.rstrip("\n") + "\n\n" + block
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.splitext(path)[0] + ".json", "w", encoding="utf-8") as fh:
        json.dump({"environment": env or environment(), "results": results}, fh, indent=2, default=str)
