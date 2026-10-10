"""Initial load through the released APIs: the LOAD stage drives the same loaders as the delta cycles."""
import pytest
from sqlalchemy import select

from sdtf.catalog.store import RecordStore
from sdtf.demo import run_vertical_slice
from sdtf.models import MigrationRun, SapSystem, StagedRecord, TransformationException
from sdtf.runtime import target_api as tapi
from sdtf.runtime.api_load import ApiTargetLoader, build_loader
from sdtf.runtime.load import SimulatedTargetLoader
from sdtf.runtime.pipeline import start_run
from sdtf.staging import get_backend

API = "/api/v1"


def test_default_load_mode_is_api_and_slice_passes(slice_result, session):
    run = session.get(MigrationRun, slice_result["run_id"])
    assert run.metrics["load_mode"] == "api" and run.status == "COMPLETED" and run.report["reconciliation"]["overall"] == "PASS"
    ld = next(st.metrics for st in run.stages if st.name == "LOAD")
    assert ld["api"]["transport"] == "SIMULATED_S4" and ld["api"]["failures"] == 0 and ld["rejected"] == 0 and ld["unsupported"] == 0 and ld["conflicts"] == 0
    assert set(ld["by_method"]) == {"API", "MIGRATION_COCKPIT", "CONFIG_TRANSPORT"} and ld["by_method"]["API"] > 0 and ld["by_method"]["MIGRATION_COCKPIT"] > 0
    calls = ld["by_api_call"]
    assert any(k.startswith("API_SALES_ORDER_SRV POST A_SalesOrder (deep insert)") for k in calls) and "API_JOURNALENTRY_SRV JournalEntryBulkCreateRequestConfirmation_In" in calls
    assert any(k.startswith("API_BUSINESS_PARTNER POST A_BusinessPartner") for k in calls) and any(k.startswith("MIGRATION_COCKPIT POST SD.BillingDocument") for k in calls)
    assert any("historical A_SalesOrder" in k for k in calls) and any("historical A_OutbDeliveryHeader" in k for k in calls)
    # the target assigned accounting document numbers; staging and target agree on them
    assert ld["assigned_keys"] > 0
    tgt = RecordStore.load(session, slice_result["target_id"], tables=["BKPF", "BSEG", "BSID"])
    staged = session.execute(select(StagedRecord).where(StagedRecord.run_id == run.id, StagedRecord.table_name == "BKPF", StagedRecord.load_status == "LOADED")).scalars().all()
    assert staged and all(tgt.by_key("BKPF", s.target_key) == s.target_payload and s.target_key != s.record_key for s in staged)
    assert all(s.target_payload.get(tapi.SOURCE_REF, "").endswith(":" + s.record_key) for s in staged)  # idempotency reference (sending system + source key) kept on the entry
    assert all(l["rule"] != "load" or l["from"] in ("API", "MIGRATION_COCKPIT") for s in staged for l in s.lineage)
    assert session.execute(select(TransformationException).where(TransformationException.run_id == run.id, TransformationException.stage == "LOAD")).scalars().first() is None


def test_rerun_is_idempotent_through_the_apis(slice_result, session):
    """A second run of the same manifest finds every document in the target: nothing is posted twice, journal
    entries are found through their source reference, histories are matched by the cockpit's idempotent keys."""
    run2 = start_run(session, slice_result["project_id"], slice_result["manifest_id"], slice_result["ruleset_id"], "operator")
    ld = next(st.metrics for st in run2.stages if st.name == "LOAD")
    assert run2.status == "COMPLETED" and ld["conflicts"] == 0 and ld["rejected"] == 0 and ld["api"]["failures"] == 0
    assert ld["skipped_duplicate"] > 0 and "API_JOURNALENTRY_SRV POST JournalEntryBulkCreateRequestConfirmation_In" not in ld["api"]["by_operation"]
    assert ld["api"]["by_operation"]["API_JOURNALENTRY_SRV POST JournalEntryLookup"] > 0
    tgt_before = RecordStore.load(session, slice_result["target_id"], tables=["BKPF"]).count("BKPF")
    run3 = start_run(session, slice_result["project_id"], slice_result["manifest_id"], slice_result["ruleset_id"], "operator")
    assert RecordStore.load(session, slice_result["target_id"], tables=["BKPF"]).count("BKPF") == tgt_before and run3.report["reconciliation"]["overall"] == "PASS"


def test_direct_load_mode_still_available_and_equivalent(session):
    out = run_vertical_slice(session, scale=1, seed=17, connector="SYNTHETIC")
    run_api = out["run"]
    assert run_api.metrics["load_mode"] == "api" and run_api.report["reconciliation"]["overall"] == "PASS"
    base = session.get(MigrationRun, run_api.id)
    run_direct = start_run(session, base.project_id, base.manifest_id, base.ruleset_id, "operator", load_mode="direct")
    ld = next(st.metrics for st in run_direct.stages if st.name == "LOAD")
    assert run_direct.metrics["load_mode"] == "direct" and "api" not in ld and run_direct.status == "COMPLETED"
    assert isinstance(build_loader(session, session.get(SapSystem, base.target_system_id), run_direct.id, load_mode="direct"), SimulatedTargetLoader)
    assert isinstance(build_loader(session, session.get(SapSystem, base.target_system_id), run_direct.id), ApiTargetLoader)


def test_api_loader_classifies_existing_documents(session):
    """Concurrent partitions or a crashed load: an existing identical document is a duplicate, a changed one a
    conflict; nothing is written twice."""
    out = run_vertical_slice(session, scale=1, seed=19, connector="RFC")
    run, tgt = out["run"], out["target"]
    backend = get_backend(session=session)
    recs = [r for r in backend.iter_records(run.id, table="VBAK") if r.load_status == "LOADED" and r.target_payload.get("GBSTK") != "C"]
    assert recs
    sample = recs[0]
    items = [r for r in backend.iter_records(run.id, table="VBAP") if r.record_key.startswith(sample.record_key + "|")]
    # reset the staged records to TRANSFORMED and reload: duplicates
    for r in [sample, *items]:
        r.load_status = "TRANSFORMED"
    backend.update_records(run.id, [sample, *items])
    loader = ApiTargetLoader(session, tgt, run.id, backend=backend)
    m = loader.load(partition=sample.partition)
    assert m["skipped_duplicate"] >= 1 + len(items) and m["conflicts"] == 0 and m["loaded"] == 0
    # change the staged header content and reload: conflict with an exception, target untouched
    sample.load_status = "TRANSFORMED"
    sample.target_payload = {**sample.target_payload, "AUDAT": "19990101"}
    backend.update_records(run.id, [sample])
    before = RecordStore.load(session, tgt.id, tables=["VBAK"]).by_key("VBAK", sample.target_key)
    m = ApiTargetLoader(session, tgt, run.id, backend=backend).load(partition=sample.partition)
    assert m["conflicts"] == 1
    assert RecordStore.load(session, tgt.id, tables=["VBAK"]).by_key("VBAK", sample.target_key) == before
    ex = session.execute(select(TransformationException).where(TransformationException.run_id == run.id, TransformationException.stage == "LOAD")).scalars().all()
    assert any("different content" in e.message for e in ex)


def test_http_target_refuses_cockpit_objects_with_explanation(monkeypatch):
    import httpx

    def handler(request):
        if request.headers.get("x-csrf-token") == "fetch":
            return httpx.Response(200, json={"d": {}}, headers={"x-csrf-token": "t"})
        return httpx.Response(201, json={"d": {"SalesOrder": "1"}})

    t = tapi.S4ApiHttpTransport({"base_url": "https://s4.example.com", "user": "u", "passwd": "p"}, client=httpx.Client(transport=httpx.MockTransport(handler)))
    c = tapi.TargetApiClient(t)
    with pytest.raises(tapi.ApiError, match="Migrate Your Data"):
        c.cockpit_load("SD.BillingDocument", [("VBRK", {"VBELN": "1"})])
    assert c.stats()["failures"] == 1


def test_runs_api_accepts_load_mode(client, tokens, session):
    """`load_mode` is validated and stored. Direct and API loads must not be mixed on one target (their keys differ
    for target-numbered documents), so the direct run goes to its own project."""
    out = run_vertical_slice(session, scale=1, seed=23, connector="SYNTHETIC")
    base = out["run"]
    session.commit()  # the API uses its own session
    arch = tokens["architect"]
    r = client.post(f"{API}/projects/{base.project_id}/runs", json={"manifest_id": base.manifest_id, "ruleset_id": base.ruleset_id, "mode": "SIMULATED", "load_mode": "direct"}, headers=arch)
    assert r.status_code == 201 and r.json()["metrics"]["load_mode"] == "direct"
    r = client.post(f"{API}/projects/{base.project_id}/runs", json={"manifest_id": base.manifest_id, "ruleset_id": base.ruleset_id, "mode": "SIMULATED", "load_mode": "bulk"}, headers=arch)
    assert r.status_code == 422
