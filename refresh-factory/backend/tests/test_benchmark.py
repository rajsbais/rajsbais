import random

import pytest

from rfactory.benchmark import service as bsvc
from rfactory.benchmark.model import NoModel, fit
from rfactory.security.auth import DEMO_USERS as U, Forbidden
from rfactory.service import Conflict, RefreshService

from .conftest import ADMIN, ALICE, BOB, CAROL, approved_project, ready_project
from .test_persistence import restart

AGENT = U["refresh.copilot"]
SIZES = [100, 200, 400, 800, 1600, 3200]


def line(a, b, noise=0.0, seed=1, sizes=SIZES, reps=2):
    rng = random.Random(seed)
    return [(n, a + b * n + rng.gauss(0, noise)) for n in sizes for _ in range(reps)]


# ---------------------------------------------------------------- model maths
def test_fit_recovers_a_known_law():
    m = fit(line(2.0, 0.01, noise=0.0))
    assert m.a == pytest.approx(2.0, abs=1e-6) and m.b == pytest.approx(0.01, rel=1e-6)
    assert m.rows_per_second == pytest.approx(100, rel=1e-6) and m.quality == "good" and m.r2 == pytest.approx(1.0)


def test_prediction_interval_covers_about_its_nominal_rate():
    hit = tot = 0
    for seed in range(60):
        rng = random.Random(seed)
        m = fit(line(1.0, 0.02, noise=0.5, seed=seed))
        for n in (300, 900, 2000):
            truth = 1.0 + 0.02 * n + rng.gauss(0, 0.5)
            p = m.predict(n)
            hit += p["low"] <= truth <= p["high"]
            tot += 1
    assert 0.70 <= hit / tot <= 0.92, hit / tot  # nominal 80%


def test_interval_widens_when_extrapolating_and_away_from_the_data():
    m = fit(line(1.0, 0.02, noise=0.5))
    inside, far = m.predict(1000), m.predict(100000)
    assert not inside["extrapolated"] and far["extrapolated"] and far["half_width"] > 5 * inside["half_width"]


@pytest.mark.parametrize("pts,why", [
    (line(1, 0.01, sizes=[100, 200], reps=3), "different sizes"),
    (line(1, 0.01, sizes=[100, 110, 120, 130, 140]), "span"),
    (line(1, 0.01, sizes=[100, 400, 800], reps=1), "usable sample"),
    ([(n, 5.0 - 0.001 * n) for n in SIZES], "not positive"),
])
def test_fit_refuses_data_that_cannot_support_a_model(pts, why):
    with pytest.raises(NoModel) as e:
        fit(pts)
    assert why in e.value.reason


def test_robust_pass_drops_a_gross_outlier_and_reports_it():
    pts = line(1.0, 0.01, noise=0.05, sizes=SIZES, reps=3)
    pts[4] = (pts[4][0], pts[4][1] + 500)
    m = fit(pts)
    assert m.dropped == 1 and m.b == pytest.approx(0.01, rel=0.1)


def test_leave_one_out_error_marks_noisy_data_as_poor_quality():
    m = fit(line(0.1, 0.01, noise=3.0, seed=3))
    assert m.quality in ("fair", "poor") and m.mape > 0.25


# ---------------------------------------------------------------- service
def feed(b, phase, env, a=0.5, slope=0.001, **kw):
    for n, s in line(a, slope, noise=slope * 20, sizes=[500, 1000, 2000, 4000, 8000], reps=1):
        b.add(phase, n, n * 100, s, env, kw.get("origin", "harness"))


def test_uncalibrated_estimate_is_the_placeholder_and_says_so(svc):
    p = ready_project(svc)
    e = svc.plan_summary(p.id)["estimate"]
    assert e["basis"] == "placeholder" and e["model_assumption"] and e["low"] is None
    assert any(m["phase"] == "reconcile" for m in e["missing"]) and e["seconds"] == e["placeholder_seconds"]


def test_calibrated_only_when_every_phase_has_a_model_in_the_right_environment(svc):
    b = svc.bench
    for ph, env in (("extract", "simulated"), ("mask_stage", "platform"), ("load", "simulated")):
        feed(b, ph, env)
    e = b.estimate(3000, 300000, svc.src_id, svc.tgt_id)
    assert e["basis"] == "placeholder" and [m["phase"] for m in e["missing"]] == ["reconcile"]
    feed(b, "reconcile", "simulated")
    e = b.estimate(3000, 300000, svc.src_id, svc.tgt_id)
    assert e["basis"] == "calibrated" and not e["model_assumption"] and e["low"] < e["seconds"] < e["high"]
    assert e["seconds"] == pytest.approx(sum(0.5 + 0.001 * 3000 for _ in range(4)), rel=0.1)
    assert "not SAP" in e["environment_note"]


def test_a_model_never_answers_for_another_environment(svc):
    b = svc.bench
    for ph, env in (("extract", "remote:PRDREAD"), ("mask_stage", "platform"), ("load", "simulated"), ("reconcile", "simulated")):
        feed(b, ph, env)
    e = b.estimate(3000, 0, svc.src_id, svc.tgt_id)  # source is simulated, samples are for a remote profile
    assert e["basis"] == "placeholder" and e["missing"][0]["environment"] == "simulated"


def test_environment_labels_and_plausibility_guard(svc):
    b = svc.bench
    spec = {"phase": "load", "rows": 10000, "seconds": 20, "environment": "real:QAS-copy-1"}
    s = b.import_sample(ALICE, spec)
    assert s.declared and s.origin == "import" and s.environment == "real:QAS-copy-1"
    for bad in ("simulated", "platform", ""):
        with pytest.raises(Conflict):
            b.import_sample(ALICE, {**spec, "environment": bad})
    with pytest.raises(Conflict):
        b.import_sample(ALICE, {**spec, "seconds": 0.0001})  # 100M rows/s
    with pytest.raises(Conflict):
        b.import_sample(ALICE, {**spec, "phase": "teleport"})
    with pytest.raises(Conflict):
        b.import_sample(ALICE, {**spec, "rows": 0})


def test_import_bind_exclude_need_a_human_with_permission(svc):
    spec = {"phase": "load", "rows": 100, "seconds": 1, "environment": "real:x"}
    for who in (AGENT, U["viewer.audit"] if "viewer.audit" in U else AGENT):
        with pytest.raises(Forbidden):
            svc.bench.import_sample(who, spec)
        with pytest.raises(Forbidden):
            svc.bench.bind(who, svc.src_id, "real:x")
    s = svc.bench.import_sample(ALICE, spec)
    with pytest.raises(Forbidden):
        svc.bench.exclude(AGENT, s.id)
    assert svc.bench.exclude(ALICE, s.id).excluded and not svc.bench.usable("load", "real:x")


def test_excluded_samples_do_not_feed_models(svc):
    b = svc.bench
    feed(b, "load", "simulated")
    assert b.model("load", "simulated")[0] is not None
    for s in b.samples[:2]:
        s.excluded = True
    assert b.model("load", "simulated")[0] is None


def test_binding_moves_a_systems_samples_to_its_own_environment(svc):
    svc.bench.bind(ALICE, svc.tgt_id, "real:EQ1-copy")
    assert svc.bench.environment_of(svc.tgt_id) == "real:EQ1-copy" and svc.bench.environment_of(svc.src_id) == "simulated"
    with pytest.raises(Conflict):
        svc.bench.bind(ALICE, svc.tgt_id, "simulated")


# ---------------------------------------------------------------- recording from real work
def test_plan_builds_and_clean_runs_record_samples(svc):
    p = approved_project(svc)
    n0 = [s.phase for s in svc.bench.samples]
    assert n0 == ["extract"]  # the plan build only
    run = svc.execute(ALICE, p.id)
    phases = {s.phase for s in svc.bench.samples if s.run_id == run.id}
    assert run.status == "COMPLETED" and phases == {"mask_stage", "load", "reconcile"}
    assert all(s.environment == "platform" for s in svc.bench.samples if s.phase == "mask_stage")


def test_failed_or_retried_runs_are_not_throughput_evidence(svc):
    p = approved_project(svc)
    before = len(svc.bench.samples)

    def boom(op, table):
        if op == "upsert":
            raise RuntimeError("permanent target failure")
    run = svc.execute(ALICE, p.id, fault_injector=boom)
    assert run.status == "FAILED" and len(svc.bench.samples) == before


def test_resumed_runs_are_not_throughput_evidence(svc):
    from datetime import datetime, timedelta
    t0 = datetime(2026, 1, 1)
    steps = [{"name": n, "started": t0.isoformat(), "finished": (t0 + timedelta(seconds=5)).isoformat()} for n in ("EXTRACT_MASK_STAGE", "LOAD")]
    proj = type("P", (), {"id": "x", "name": "x", "target_id": svc.tgt_id, "plan": type("Pl", (), {"summary": lambda self: {"bytes": 10, "total_rows": 3}})()})()
    mk = lambda att: type("R", (), {"id": "r", "status": "COMPLETED", "attempts": att, "steps": steps, "staged": {"i": {"T": [{}, {}, {}]}}})()
    svc.bench.record_run(proj, mk(2), 1.0)
    assert svc.bench.samples == []
    svc.bench.record_run(proj, mk(1), 1.0)
    assert {s.phase for s in svc.bench.samples} == {"mask_stage", "load", "reconcile"}


def test_agent_principals_cannot_write_benchmarks_even_with_the_permission(svc):
    from rfactory.security.auth import Principal
    bot = Principal("bot", "bot", ("basis", "admin"), kind="service")
    spec = {"phase": "load", "rows": 100, "seconds": 1, "environment": "real:x"}
    assert bot.can("system:write") or bot.can("run:execute")
    with pytest.raises(Forbidden):
        svc.bench.import_sample(bot, spec)
    with pytest.raises(Forbidden):
        svc.bench.run_benchmark(bot, svc.src_id, windows=(30,), repeats=1)


def test_harness_respects_system_scope(svc):
    from rfactory.security.auth import Principal
    scoped = Principal("scoped", "scoped", ("basis",), attrs={"systems": [svc.s4_src]})
    assert scoped.can("run:execute")
    with pytest.raises(Forbidden):
        svc.bench.run_benchmark(scoped, svc.src_id, windows=(30,), repeats=1)


def test_blocked_plan_records_nothing(svc):
    n = len(svc.bench.samples)
    svc.bench.record_run(type("P", (), {"id": "x"})(), type("R", (), {"status": "FAILED", "attempts": 1})(), 1.0)
    assert len(svc.bench.samples) == n


def test_accuracy_tracks_predicted_against_actual_once_calibrated(svc):
    b = svc.bench
    for ph, env in (("extract", "simulated"), ("mask_stage", "platform"), ("load", "simulated"), ("reconcile", "simulated")):
        feed(b, ph, env, a=0.0, slope=0.00001)
    p = approved_project(svc)
    shown = svc.plan_summary(p.id)["estimate"]
    assert shown["basis"] == "calibrated"
    run = svc.execute(ALICE, p.id)
    a = b.accuracy[-1]
    assert a["run_id"] == run.id and a["predicted"] > 0 and a["actual"] >= 0
    assert (a["error"] is not None) == (a["actual"] > 0)  # a run too fast for the clock to register has no relative error (seen once under load)
    assert b.summary()["accuracy"]["runs"] == 1


def test_placeholder_estimates_are_not_scored(svc):
    p = approved_project(svc)
    svc.execute(ALICE, p.id)
    assert svc.bench.accuracy == []


# ---------------------------------------------------------------- harness
class Tick:
    """Deterministic clock: each reading advances by the amount the code under test 'costs', proportional to nothing, so the harness's own bookkeeping is tested."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.01
        return self.t


def test_harness_measures_growing_scopes_and_builds_models(svc):
    out = svc.bench.run_benchmark(ALICE, svc.src_id, windows=(15, 30, 60, 90, 120, 180), repeats=2)
    assert out["samples_added"] >= 30 and out["environment"] == "simulated"
    kinds = {(m["phase"], m["environment"]) for m in out["models"]}
    assert {("extract", "simulated"), ("mask_stage", "platform"), ("load", "simulated")} <= kinds
    sizes = {s.rows for s in svc.bench.samples if s.phase == "load"}
    assert len(sizes) >= 3 and max(sizes) / min(sizes) >= 2


def test_harness_never_writes_to_the_real_target_and_needs_run_permission(svc):
    before = svc.adapters[svc.tgt_id].table_counts()
    svc.bench.run_benchmark(ALICE, svc.src_id, windows=(30, 60), repeats=1)
    assert svc.adapters[svc.tgt_id].table_counts() == before
    for who in (AGENT, CAROL):
        with pytest.raises(Forbidden):
            svc.bench.run_benchmark(who, svc.src_id, windows=(30,), repeats=1)


def test_full_benchmark_plus_runs_calibrate_the_estimate(svc):
    svc.bench.run_benchmark(ALICE, svc.src_id, windows=(15, 30, 60, 90, 120, 180), repeats=3)
    for days in (20, 40, 70, 90, 150):  # reconcile is sampled only by real runs, so do a few of different sizes
        p = approved_project(svc, days=days)
        svc.execute(ALICE, p.id)
    assert {s.phase for s in svc.bench.samples} == set(bsvc.PHASES)
    for i, s in enumerate(svc.bench.samples):  # host timings are noise on a shared CI machine: replace them with a known law, keeping sizes and wiring
        s.seconds = 0.01 + s.rows * 1e-4 * (1 + 0.02 * ((i % 5) - 2))
    p = approved_project(svc)
    e = svc.plan_summary(p.id)["estimate"]
    assert e["basis"] == "calibrated" and e["low"] <= e["seconds"] <= e["high"] and set(e["phases"]) == set(bsvc.PHASES)
    assert e["seconds"] == pytest.approx(4 * (0.01 + p.plan.summary()["total_rows"] * 1e-4), rel=0.3)


# ---------------------------------------------------------------- persistence and HTTP
def test_samples_models_and_bindings_survive_a_restart(tmp_path):
    s = RefreshService(tmp_path, persist=True)
    b = s.bootstrap_demo(ADMIN)
    s.src_id, s.tgt_id, s.s4_src, s.s4_tgt = b["source"]["id"], b["target"]["id"], b["s4_source"]["id"], b["s4_target"]["id"]
    feed(s.bench, "load", "simulated")
    s.bench.bind(ALICE, s.tgt_id, "real:EQ1-copy")
    m0 = s.bench.model("load", "simulated")[0].b
    t = restart(s)
    assert len(t.bench.samples) == 5 and t.bench.bindings == {s.tgt_id: "real:EQ1-copy"}
    assert t.bench.model("load", "simulated")[0].b == pytest.approx(m0)


def test_http_api_and_roles(tmp_path):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    c = TestClient(create_app(tmp_path))
    H = lambda u: {"X-Demo-User": u}
    b = c.post("/api/demo/bootstrap", headers=H("root.admin")).json()
    src, tgt = b["source"]["id"], b["target"]["id"]
    assert c.get("/api/benchmark/summary", headers=H("carol.approver")).json()["samples"] == 0
    assert c.post("/api/benchmark/run", json={"source_id": src, "windows": [20, 40, 80, 160], "repeats": 2}, headers=H("refresh.copilot")).status_code == 403
    assert c.post("/api/benchmark/run", json={"source_id": src, "windows": list(range(1, 20))}, headers=H("alice.basis")).status_code == 422
    r = c.post("/api/benchmark/run", json={"source_id": src, "windows": [20, 40, 80, 160], "repeats": 2}, headers=H("alice.basis"))
    assert r.status_code == 200 and r.json()["samples_added"] >= 18
    smp = c.get("/api/benchmark/samples?phase=load", headers=H("carol.approver")).json()
    assert smp and all(s["phase"] == "load" for s in smp)
    imp = {"phase": "extract", "rows": 5000, "seconds": 12, "environment": "real:QAS-copy-1"}
    assert c.post("/api/benchmark/samples", json=imp, headers=H("refresh.copilot")).status_code == 403
    assert c.post("/api/benchmark/samples", json={**imp, "environment": "simulated"}, headers=H("alice.basis")).status_code == 409
    sid = c.post("/api/benchmark/samples", json=imp, headers=H("alice.basis")).json()["id"]
    assert c.post(f"/api/benchmark/samples/{sid}/exclude", headers=H("alice.basis")).json()["excluded"]
    e = c.post("/api/benchmark/estimate", json={"rows": 4000, "source_id": src, "target_id": tgt}, headers=H("carol.approver")).json()
    assert e["basis"] == "placeholder" and e["missing"]  # reconcile has no samples yet
    assert c.post("/api/benchmark/bind", json={"system_id": tgt, "environment": "real:x"}, headers=H("refresh.copilot")).status_code == 403
    cora = c.post("/api/benchmark/estimate", json={"rows": 10, "source_id": b["s4_source"]["id"]}, headers=H("cora.regional"))
    assert cora.status_code == 403
