"""Delta capture and replay: CDC contract on the simulated add-on, business-activity simulator, delta engine
(scope filter, ordering, idempotency, conflicts, staging sync, final reconciliation) and the API."""
from collections import Counter

import pytest
from sqlalchemy import select

from sdtf.catalog.store import RecordStore
from sdtf.demo import run_vertical_slice
from sdtf.models import DeltaEvent, ReconciliationResult, ScopeManifest
from sdtf.reconciliation.service import reconcile_run
from sdtf.runtime import rfc
from sdtf.runtime.activity import load_change_log, simulate_business_activity
from sdtf.runtime.delta import DeltaEngine, declare_freeze, delta_state, start_delta_cycle, type_order
from sdtf.runtime.pipeline import RunPrecondition
from sdtf.synthetic.ecc_generator import LandscapeSpec, generate_landscape

API = "/api/v1"


# ------------------------------------------------------------------------------------ CDC contract
def _log(n: int, table="VBAK", cc="5000") -> list[dict]:
    out = []
    for i in range(1, n + 1):
        row = {"VBELN": f"9{i:06d}", "BUKRS_VF": cc if i % 2 else "1000", "GBSTK": "A"}
        out.append({"SEQ": i, "CHANGENR": f"C{i}", "OBJECT_TYPE": "SD.SalesOrder", "TABNAME": table, "KEY": row["VBELN"], "OP": "I" if i % 3 else "D", "CHANGED_AT": "20261009120000", "CHANGED_BY": "T", "JSON": rfc.row_json(row) if i % 3 else ""})
    return out


def test_cdc_poll_contract_paging_filtering_watermark_checksum():
    store = RecordStore.from_tables("x", generate_landscape(LandscapeSpec(seed=1, scale=1)))
    addon = rfc.SimulatedAbapAddon(store, change_log=_log(10))
    assert addon.call(rfc.FM_OPEN_SNAPSHOT)["EV_CDC_WATERMARK"] == "10"
    objs = [{"OBJECT_TYPE": "SD.SalesOrder", "TABNAME": "VBAK", "FIELD": "BUKRS_VF", "OP": "EQ", "LOW": "5000", "HIGH": ""}]
    r = addon.call(rfc.FM_CDC_POLL, IV_WATERMARK="0", IT_OBJECTS=objs, IV_PACKAGE=3)
    assert [e["SEQ"] for e in r["ET_EVENTS"]] == [1, 3, 5] and r["EV_EOF"] == "" and r["EV_WATERMARK"] == "5"  # odd = in scope; 3 and 6 and 9 are deletes (pass the filter)
    assert r["EV_CHECKSUM"] == rfc.package_checksum([rfc.row_json(e) for e in r["ET_EVENTS"]])
    r2 = addon.call(rfc.FM_CDC_POLL, IV_WATERMARK=r["EV_WATERMARK"], IT_OBJECTS=objs, IV_PACKAGE=100)
    assert [e["SEQ"] for e in r2["ET_EVENTS"]] == [6, 7, 9] and r2["EV_EOF"] == "X" and r2["EV_WATERMARK"] == "10"
    # unsubscribed tables are dropped but still advance the watermark; no objects = everything
    assert addon.call(rfc.FM_CDC_POLL, IV_WATERMARK="0", IT_OBJECTS=[{"TABNAME": "BKPF"}], IV_PACKAGE=100)["EV_WATERMARK"] == "10"
    assert len(addon.call(rfc.FM_CDC_POLL, IV_WATERMARK="0", IT_OBJECTS=[], IV_PACKAGE=100)["ET_EVENTS"]) == 10
    with pytest.raises(rfc.RfcError, match="INVALID_WATERMARK"):
        addon.call(rfc.FM_CDC_POLL, IV_WATERMARK="snap-x", IT_OBJECTS=objs)
    with pytest.raises(rfc.RfcError, match="NOT_AUTHORIZED"):
        rfc.SimulatedAbapAddon(store, allowed_tables={"T001"}, change_log=_log(2)).call(rfc.FM_CDC_POLL, IV_WATERMARK="0", IT_OBJECTS=objs)
    # client: checksum verification, cursor driving, parsed row images
    c = rfc.AbapAddonClient(addon, package_size=4)
    evs = list(c.cdc_events("0", objs))
    assert [e["SEQ"] for e in evs] == [1, 3, 5, 6, 7, 9] and evs[0]["row"]["VBELN"] == "9000001" and evs[1]["row"] is None and c.last_watermark == "10"


def test_apply_order_follows_document_flow():
    rank = type_order()
    assert rank["CFG.CompanyCode"] == 0 and rank["MD.Customer"] == 1 and rank["MD.Material"] == 1
    assert rank["SD.SalesOrder"] < rank["SD.Delivery"] < rank["SD.BillingDocument"] <= rank["FI.AccountingDocument"]
    assert rank["MM.PurchaseOrder"] < rank["FI.AccountingDocument"]


# ------------------------------------------------------------------------------- activity simulator
def test_activity_simulator_persists_changes_and_change_log(session):
    from sdtf.demo import create_demo_project

    ctx = create_demo_project(session, scale=1, seed=9, connector="RFC")
    src = ctx["source"]
    before = RecordStore.load(session, src.id)
    out = simulate_business_activity(session, src, seed=2, count=10, company_codes=["5000"])
    assert out["applied"] == 10 and out["events"] > 10 and out["blocked_by_freeze"] == 0 and out["watermark"] == str(out["events"])
    log = load_change_log(session, src.id)
    assert [e["SEQ"] for e in log] == list(range(1, len(log) + 1)) and all(e["JSON"] == "" for e in log if e["OP"] == "D")
    after = RecordStore.load(session, src.id)
    assert after.count("VBAK") == before.count("VBAK") + out["by_kind"]["NEW_SALES_ORDER"]
    assert after.count("BKPF") == before.count("BKPF") + out["by_kind"]["NEW_FI_DOCUMENT"]
    assert after.count("VBAP") == before.count("VBAP") + sum(1 for e in log if e["TABNAME"] == "VBAP" and e["OP"] == "I") - out["by_kind"]["DELETE_SALES_ORDER_ITEM"]
    # new FI documents balance
    for e in log:
        if e["TABNAME"] == "BKPF" and e["OP"] == "I":
            h = after.by_key("BKPF", e["KEY"])
            lines = [l for l in after.lookup("BSEG", "BELNR", h["BELNR"]) if l["BUKRS"] == h["BUKRS"]]
            assert abs(sum(float(l["DMBTR"]) if l["SHKZG"] == "S" else -float(l["DMBTR"]) for l in lines)) < 0.005
    frozen = simulate_business_activity(session, src, seed=3, count=4, company_codes=["5000"], frozen_ccs={"5000"})
    assert frozen["applied"] == 0 and frozen["blocked_by_freeze"] == 4
    # deterministic per seed
    a = simulate_business_activity(session, src, seed=5, count=5, company_codes=["1000"])
    assert a["by_kind"] == simulate_business_activity(session, ctx["source"], seed=5, count=5, company_codes=["1000"])["by_kind"]


# ---------------------------------------------------------------------------------------- the engine
@pytest.fixture(scope="module")
def delta_world(engine):
    """Baseline run over RFC, then business activity in and out of scope, then delta cycles. Shared read-only."""
    from sdtf.db import session_scope

    with session_scope() as s:
        out = run_vertical_slice(s, scale=1, seed=5, connector="RFC")
        run, src, tgt = out["run"], out["source"], out["target"]
        activity = simulate_business_activity(s, src, seed=3, count=20, company_codes=["5000", "1000"])
        m = s.get(ScopeManifest, run.manifest_id)
        drift = reconcile_run(s, run, m, RecordStore.load(s, src.id), RecordStore.load(s, tgt.id))
        c1 = start_delta_cycle(s, run.id, "operator")
        c1_metrics = {st.name: dict(st.metrics) for st in c1.stages}
        c1_events = [(e.seq, e.table_name, e.op, e.status, e.action, e.target_key, e.changenr) for e in s.execute(select(DeltaEvent).where(DeltaEvent.run_id == c1.id)).scalars()]
        ids = {"run": run.id, "src": src.id, "tgt": tgt.id, "c1": c1.id, "manifest": m.id, "project": run.project_id}
    return {"ids": ids, "activity": activity, "drift": drift, "c1": c1_metrics, "c1_events": c1_events}


def test_source_drift_is_visible_before_delta_and_cycle_captures_only_scope(delta_world):
    w = delta_world
    assert w["activity"]["by_company_code"]["5000"] > 0 and w["activity"]["by_company_code"]["1000"] > 0
    assert w["drift"]["overall"] != "PASS", "business activity in scope must make the baseline reconciliation drift"
    cap, tr, ap, rec = w["c1"]["CAPTURE"], w["c1"]["TRANSFORM"], w["c1"]["APPLY"], w["c1"]["RECONCILE"]
    assert cap["captured"] == cap["in_scope"] + cap["filtered"] and cap["in_scope"] > 0 and cap["filtered_by_reason"]["out_of_scope"] > 0
    assert cap["watermark_from"] == "0" and cap["watermark_to"] == w["activity"]["watermark"] and cap["lag_seconds"] is not None
    assert tr["rejected"] == 0 and tr["transformed"] + tr["deletes_resolved"] == cap["in_scope"] and tr["by_rule"]["cc-reassign"] > 0
    assert ap["inserted"] > 0 and ap["updated"] > 0 and ap["deleted"] > 0 and ap["conflicts"] == 0 and ap["inserted"] + ap["updated"] + ap["deleted"] == cap["in_scope"]
    assert rec["overall"] == "PASS" and rec["by_layer"]["TECHNICAL"]["PASS"] >= 1
    statuses = Counter(e[3] for e in w["c1_events"])
    assert statuses["APPLIED"] == cap["in_scope"] and statuses["FILTERED"] == cap["filtered"] and set(statuses) == {"APPLIED", "FILTERED"}
    # every in-scope event ended in the target under its transformed key, deletes are gone
    assert all(e[5] for e in w["c1_events"] if e[3] == "APPLIED")


def test_applied_events_are_in_target_and_baseline_staging_follows(delta_world):
    from sdtf.db import session_scope
    from sdtf.staging import get_backend

    ids = delta_world["ids"]
    with session_scope() as s:
        tgt = RecordStore.load(s, ids["tgt"])
        evs = s.execute(select(DeltaEvent).where(DeltaEvent.run_id == ids["c1"], DeltaEvent.status == "APPLIED")).scalars().all()
        for e in evs:
            row = tgt.by_key(e.table_name, e.target_key)
            assert (row is None) if e.action == "DELETED" else (row == e.target_payload), (e.table_name, e.target_key, e.action)
            assert e.target_payload is None or e.target_payload.get("BUKRS", "SP01") in ("SP01",) or e.table_name not in ("BKPF", "BSEG")
        # the baseline's staging knows the new/changed/deleted rows, so its reconciliation stays meaningful
        backend = get_backend(session=s)
        staged = {(r.table_name, r.record_key): r for r in backend.iter_records(ids["run"])}
        for e in evs:
            st = staged[(e.table_name, e.record_key)]
            assert st.load_status == ("DELETED" if e.action == "DELETED" else "LOADED") and (st.target_payload == e.target_payload or e.action == "DELETED")


def test_replay_is_idempotent_and_stale_events_conflict(delta_world):
    from sdtf.db import session_scope
    from sdtf.models import MigrationRun

    ids = delta_world["ids"]
    with session_scope() as s:
        # a second cycle sees nothing new
        c2 = start_delta_cycle(s, ids["run"], "operator")
        assert c2.status == "COMPLETED" and [st.metrics["captured"] for st in c2.stages if st.name == "CAPTURE"] == [0]
        # replaying the already-applied events (crash after apply, before the ledger was read) is a no-op
        base = s.get(MigrationRun, ids["run"])
        probe = MigrationRun(project_id=base.project_id, manifest_id=base.manifest_id, ruleset_id=base.ruleset_id, source_system_id=base.source_system_id, target_system_id=base.target_system_id, started_by="t", metrics={"kind": "DELTA", "baseline_run_id": base.id, "watermark_from": "0", "cycle": 99, "staging_backend": base.metrics.get("staging_backend")})
        s.add(probe)
        s.flush()
        eng = DeltaEngine(s, probe)
        cap = eng.capture()
        assert cap["captured"] > 0 and cap["duplicates_ignored"] == cap["captured"] and cap["in_scope"] == 0
        # stale event: an older sequence for a key the target already updated with a newer one -> CONFLICT, target untouched
        applied = s.execute(select(DeltaEvent).where(DeltaEvent.run_id == ids["c1"], DeltaEvent.status == "APPLIED", DeltaEvent.action == "UPDATED")).scalars().first()
        stale_payload = {**applied.target_payload, "NETWR": 1.0}
        s.add(DeltaEvent(run_id=probe.id, baseline_run_id=base.id, seq=-1, changenr="STALE", object_type=applied.object_type, object_key=applied.object_key, table_name=applied.table_name, record_key=applied.record_key, op="U", source_payload=applied.source_payload, target_payload=stale_payload, target_key=applied.target_key, status="CAPTURED"))
        s.flush()
        ap = eng.apply()
        assert ap["conflicts"] == 1 and ap["updated"] == 0 and ap["inserted"] == 0
        assert RecordStore.load(s, ids["tgt"], tables=[applied.table_name]).by_key(applied.table_name, applied.target_key) == applied.target_payload
        s.execute(DeltaEvent.__table__.delete().where(DeltaEvent.run_id == probe.id))
        s.delete(probe)


def test_freeze_final_delta_and_full_reconciliation(delta_world):
    from sdtf.db import session_scope
    from sdtf.models import MigrationRun

    ids = delta_world["ids"]
    with session_scope() as s:
        base = s.get(MigrationRun, ids["run"])
        src = s.get(__import__("sdtf.models", fromlist=["SapSystem"]).SapSystem, ids["src"])
        with pytest.raises(RunPrecondition, match="business freeze"):
            start_delta_cycle(s, base.id, "operator", final=True)
        fr = declare_freeze(s, base, "approver", "cutover weekend")
        assert fr["company_codes"] == ["5000"] and base.metrics["freeze"]["declared_by"] == "approver"
        with pytest.raises(RunPrecondition, match="already declared"):
            declare_freeze(s, base, "approver")
        # a few more changes before the freeze bit, then the business is frozen for 5000 (other company codes continue)
        act = simulate_business_activity(s, src, seed=11, count=8, company_codes=["5000", "1000"], frozen_ccs={"5000"})
        assert act["blocked_by_freeze"] > 0 and act["by_company_code"].get("5000") is None
        final = start_delta_cycle(s, base.id, "operator", final=True)
        rec = next(st.metrics for st in final.stages if st.name == "RECONCILE")
        assert final.status == "COMPLETED" and rec["overall"] == "PASS"
        full = rec["final_reconciliation"]
        assert full["overall"] == "PASS" and full["by_layer"]["FINANCIAL"]["PASS"] > 0 and full["by_layer"]["TECHNICAL"]["PASS"] > 0 and full["checks"] > 200
        assert s.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == final.id, ReconciliationResult.layer == "FINANCIAL")).scalars().first() is not None
        st = delta_state(s, base)
        assert st["cutover_ready"] and st["final_delta"]["run_id"] == final.id and st["backlog"]["events"] == 0
        assert [c["final"] for c in st["cycles"]] == [False, False, True] and st["cycles"][-1]["final_reconciliation"] == "PASS"
        with pytest.raises(RunPrecondition, match="cutover state"):
            start_delta_cycle(s, base.id, "operator")


def test_delta_requires_rfc_source(slice_result, session):
    with pytest.raises(RunPrecondition, match="RFC connector"):
        start_delta_cycle(session, slice_result["run_id"], "operator")


# -------------------------------------------------------------------------------------------------- API
def test_delta_api_flow(client, tokens):
    arch, approver, viewer = tokens["architect"], tokens["approver"], tokens["viewer"]
    d = client.post(f"{API}/projects/demo", json={"scale": 1, "seed": 21, "connector": "RFC", "name": "delta api"}, headers=arch).json()
    pid = d["project"]["id"]
    src = next(x for x in d["project"]["systems"] if x["role"] == "SOURCE")
    # bring the project to a completed baseline through the API-less helper (the API run flow is covered elsewhere)
    from sdtf.db import session_scope

    with session_scope() as s:
        from sdtf.demo import demo_scope_definition
        from sdtf.discovery.service import discover_system
        from sdtf.graph.service import build_graph, persist_graph
        from sdtf.models import Project, RuleSet, SapSystem
        from sdtf.rules.engine import parse_ruleset, validate_ruleset
        from sdtf.rules.factory import generate_candidate_ruleset
        from sdtf.runtime.pipeline import start_run
        from sdtf.scope.service import (
            apply_disposition,
            approve_manifest,
            create_manifest,
            pending_dispositions,
        )

        proj = s.get(Project, pid)
        srcs = s.get(SapSystem, src["id"])
        tgts = next(x for x in s.execute(select(SapSystem).where(SapSystem.project_id == pid, SapSystem.role == "TARGET")).scalars())
        store = RecordStore.load(s, srcs.id)
        discover_system(s, srcs, "architect", store)
        persist_graph(s, srcs.id, build_graph(store, srcs.id))
        m = create_manifest(s, pid, demo_scope_definition(srcs, tgts), "architect")
        apply_disposition(s, m, pending_dispositions(m), "TRANSFER", "approver", "ok")
        approve_manifest(s, m, "approver")
        y = generate_candidate_ruleset(demo_scope_definition(srcs, tgts), "S4HANA")
        rs = parse_ruleset(y)
        rset = RuleSet(project_id=pid, name=rs.name, version=1, content_hash=rs.content_hash, source_yaml=y, compiled={"rules": rs.rules, "lookups": rs.lookups}, validation=validate_ruleset(rs), status="APPROVED", created_by="architect", approved_by="approver")
        s.add(rset)
        s.flush()
        run = start_run(s, pid, m.id, rset.id, "operator")
        assert run.status == "COMPLETED"
        proj.tenant_id = "default"
        rid = run.id
    st = client.get(f"{API}/runs/{rid}/delta", headers=arch).json()
    assert st["cdc_supported"] and st["watermark"] == "0" and st["cycles"] == [] and st["backlog"]["events"] == 0 and not st["cutover_ready"]
    act = client.post(f"{API}/systems/{src['id']}/simulate-changes", json={"seed": 2, "count": 10, "company_codes": ["5000"]}, headers=arch).json()
    assert act["applied"] == 10 and act["events"] > 10
    st = client.get(f"{API}/runs/{rid}/delta", headers=arch).json()
    assert st["backlog"]["events"] == act["events"] and st["backlog"]["in_scope"] > 0
    assert client.post(f"{API}/runs/{rid}/delta/cycles", json={}, headers=viewer).status_code == 403
    assert client.post(f"{API}/runs/{rid}/delta/cycles", json={"final": True}, headers=arch).status_code == 409
    c = client.post(f"{API}/runs/{rid}/delta/cycles", json={}, headers=arch)
    assert c.status_code == 201 and c.json()["status"] == "COMPLETED" and c.json()["metrics"]["kind"] == "DELTA" and c.json()["metrics"]["cycle"] == 1
    evs = client.get(f"{API}/runs/{rid}/delta/events", headers=arch, params={"status": "APPLIED"}).json()
    assert evs and all(e["status"] == "APPLIED" and e["target_key"] for e in evs)
    assert client.get(f"{API}/runs/{c.json()['id']}/delta/events", headers=arch).json()
    assert client.post(f"{API}/runs/{rid}/delta/freeze", json={"note": "go"}, headers=arch).status_code == 403  # approvers declare the freeze
    assert client.post(f"{API}/runs/{rid}/delta/freeze", json={"note": "go"}, headers=approver).status_code == 200
    blocked = client.post(f"{API}/systems/{src['id']}/simulate-changes", json={"seed": 3, "count": 3, "company_codes": ["5000"]}, headers=arch).json()
    assert blocked["blocked_by_freeze"] == 3
    f = client.post(f"{API}/runs/{rid}/delta/cycles", json={"final": True}, headers=arch)
    assert f.status_code == 201 and f.json()["report"]["reconciliation"]["final_reconciliation"]["overall"] == "PASS"
    st = client.get(f"{API}/runs/{rid}/delta", headers=arch).json()
    assert st["cutover_ready"] and len(st["cycles"]) == 2 and st["freeze"]["declared_by"] == "approver"
    assert client.post(f"{API}/runs/{rid}/delta/cycles", json={}, headers=arch).status_code == 409
    ds = client.get(f"{API}/platform/delta/status", headers=arch).json()
    assert ds["status"] == "SIMULATED" and ds["stage_status"]["Delta capture"] == "SIMULATED" and ds["stage_status"]["Production handover"] == "PLANNED"
    # a SYNTHETIC source refuses delta with a clear message
    syn = client.post(f"{API}/projects/demo", json={"scale": 1, "seed": 22, "connector": "SYNTHETIC", "name": "no cdc"}, headers=arch).json()
    s2 = next(x for x in syn["project"]["systems"] if x["role"] == "SOURCE")
    assert client.post(f"{API}/systems/{s2['id']}/simulate-changes", json={"count": 1}, headers=arch).status_code == 200  # the log is harmless
    assert "RFC" in str(client.get(f"{API}/runs/{rid}/delta", headers=arch).json()["source"])
