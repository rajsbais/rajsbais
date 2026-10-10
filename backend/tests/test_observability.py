"""OpenTelemetry observability: spans and metrics for inline and distributed runs; no-op when disabled."""
import json
import logging

import pytest

from sdtf import observability as obs
from sdtf.runtime.pipeline import start_run
from sdtf.runtime.worker import Worker


@pytest.fixture()
def telemetry():
    span_exporter, reader = obs.configure_in_memory()
    yield span_exporter, reader
    obs.disable()


def _metrics(reader) -> dict:
    out = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                pts = []
                for dp in m.data.data_points:
                    pts.append((dict(dp.attributes), getattr(dp, "value", None), getattr(dp, "count", None)))
                out[m.name] = pts
    return out


def test_inline_run_emits_run_and_stage_spans_and_metrics(session, slice_result, telemetry):
    span_exporter, reader = telemetry
    run = start_run(session, slice_result["project_id"], slice_result["manifest_id"], slice_result["ruleset_id"], "operator")
    assert run.status == "COMPLETED"
    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    assert "sdtf.run" in spans and spans["sdtf.run"].attributes["run_id"] == run.id
    for st in ("PRECHECK", "EXTRACT", "TRANSFORM", "LOAD", "RECONCILE", "REPORT"):
        assert f"sdtf.stage.{st}" in spans, st
        assert spans[f"sdtf.stage.{st}"].parent.span_id == spans["sdtf.run"].context.span_id, "stages are children of the run span"
    assert spans["sdtf.stage.EXTRACT"].end_time <= spans["sdtf.stage.TRANSFORM"].start_time
    m = _metrics(reader)
    assert any(a.get("status") == "COMPLETED" for a, *_ in m["sdtf.runs"])
    assert {a["stage"] for a, *_ in m["sdtf.stage.duration"]} >= {"EXTRACT", "TRANSFORM", "LOAD", "RECONCILE"}
    assert any(a.get("layer") == "FINANCIAL" and a.get("status") == "PASS" for a, *_ in m["sdtf.reconciliation.checks"])
    assert sum(v for a, v, _ in m["sdtf.stage.records"] if a["stage"] == "EXTRACT") > 1000


def test_distributed_run_emits_job_spans_and_pruning_metrics(session, slice_result, telemetry, tmp_path, monkeypatch):
    from sdtf import config
    from sdtf.db import get_session_factory

    monkeypatch.setattr(config, "settings", config.Settings(database_url=config.settings.database_url, evidence_dir=config.settings.evidence_dir, staging_backend="columnar", staging_dir=str(tmp_path / "stg")))
    span_exporter, reader = telemetry
    run = start_run(session, slice_result["project_id"], slice_result["manifest_id"], slice_result["ruleset_id"], "operator", execution="DISTRIBUTED", staging_backend="columnar")
    session.commit()
    w = Worker(get_session_factory(), "otel-w")
    w.run(until_idle=True)
    session.expire_all()
    from sdtf.models import MigrationRun

    assert session.get(MigrationRun, run.id).status == "COMPLETED"
    jobs = [s for s in span_exporter.get_finished_spans() if s.name == "sdtf.job"]
    assert {s.attributes["stage"] for s in jobs} == {"EXTRACT", "TRANSFORM", "LOAD", "RECONCILE"}
    assert all(s.attributes["worker_id"] == "otel-w" and s.attributes["run_id"] == run.id for s in jobs)
    writes = [s for s in span_exporter.get_finished_spans() if s.name == "sdtf.staging.write_partition"]
    assert writes and all(s.attributes["protocol"] in ("file", "local") and s.attributes["records"] >= 0 for s in writes)
    assert any(s.parent is not None and s.parent.span_id in {j.context.span_id for j in jobs} for s in writes), "staging writes are children of job spans"
    m = _metrics(reader)
    assert sum(v for a, v, _ in m["sdtf.jobs"] if a["status"] == "DONE") == run.metrics["partitions"] * 3 + run.metrics["stage_jobs"]["RECONCILE"]
    assert any(a["stage"] == "RECONCILE" for a, *_ in m["sdtf.stage.closed"])
    assert "sdtf.job.duration" in m and "sdtf.staging.files_pruned" in m


def test_failed_run_marks_span_error(session, slice_result, telemetry, monkeypatch):
    from sdtf.runtime import pipeline

    span_exporter, _ = telemetry
    monkeypatch.setattr(pipeline, "run_transformation", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        start_run(session, slice_result["project_id"], slice_result["manifest_id"], slice_result["ruleset_id"], "operator")
    spans = [s for s in span_exporter.get_finished_spans() if s.name in ("sdtf.run", "sdtf.stage.TRANSFORM")]
    assert len(spans) == 2 and all(s.status.status_code.name == "ERROR" for s in spans)
    assert any(e.name == "exception" for s in spans for e in s.events)


def test_json_logging_carries_trace_ids(telemetry):
    fmt = obs.JsonFormatter()
    with obs.span("sdtf.test", run_id="r1"):
        rec = logging.LogRecord("sdtf", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        rec.run_id = "r1"
        out = json.loads(fmt.format(rec))
    assert out["msg"] == "hello world" and out["run_id"] == "r1" and len(out["trace_id"]) == 32 and len(out["span_id"]) == 16


def test_disabled_telemetry_is_noop(session, slice_result):
    obs.disable()
    assert obs.status()["enabled"] is False
    with obs.span("x", a=1) as s:
        s.set_attribute("k", "v")
    obs.counter("c", 1)
    obs.histogram("h", 1.0)
    assert obs.current_trace_ids() == {}
    assert obs.setup(obs.OtelConfig(endpoint="", enabled=False, console=False))["enabled"] is False
    assert obs.instrument_app(object()) is False


def test_health_reports_telemetry_status(client):
    t = client.get("/healthz").json()["telemetry"]
    assert set(t) >= {"configured", "enabled", "exporter"}
