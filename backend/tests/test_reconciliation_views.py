"""Reconciliation through the adapters: the aggregate function module of the add-on, the source view over RFC,
the target view over the released APIs (simulated gateway and a mocked HTTPS endpoint), the integrity
evidence, unreadable tables, re-reconciliation through API and CLI."""
import json
from collections import defaultdict

import httpx
import pytest
from sqlalchemy import select

from sdtf.catalog.store import RecordStore
from sdtf.cli import main as cli_main
from sdtf.models import MigrationRun, ReconciliationResult, SapSystem, ScopeManifest
from sdtf.reconciliation import views
from sdtf.reconciliation.service import reconcile_run
from sdtf.runtime import rfc
from sdtf.runtime import target_api as tapi
from sdtf.staging import get_backend
from sdtf.synthetic.ecc_generator import LandscapeSpec, generate_landscape

API = "/api/v1"


@pytest.fixture(scope="module")
def store():
    return RecordStore.from_tables("sim", generate_landscape(LandscapeSpec(seed=7, scale=1)))


# ------------------------------------------------------------------------------------- Z_SDTF_AGGREGATE
def test_aggregate_contract(store):
    addon = rfc.SimulatedAbapAddon(store, allowed_tables={"BSEG", "BKPF", "T001K"}, snapshot_ttl=60)
    c = rfc.AbapAddonClient(addon)
    preds = [rfc.predicate("BUKRS", "EQ", "5000")]
    rows = [r for r in store.rows("BSEG") if r["BUKRS"] == "5000"]
    assert c.count("BSEG", preds) == len(rows) > 0
    agg = c.aggregate("BSEG", preds, ["BUKRS", "SHKZG"], ["DMBTR"])
    assert [a["SHKZG"] for a in agg] == ["H", "S"] and all(a["BUKRS"] == "5000" for a in agg)
    for a in agg:
        mine = [r for r in rows if r["SHKZG"] == a["SHKZG"]]
        assert a["COUNT"] == len(mine) and abs(a["SUM_DMBTR"] - round(sum(float(r["DMBTR"]) for r in mine), 2)) < 0.005
    assert c.aggregate("BSEG", [rfc.predicate("BUKRS", "EQ", "ZZZZ")], [], ["DMBTR"]) == [{"COUNT": 0, "SUM_DMBTR": 0.0}]  # empty: one zero row
    assert c.aggregate("BSEG", [rfc.predicate("BUKRS", "EQ", "ZZZZ")], ["HKONT"], []) == []
    with pytest.raises(rfc.RfcError, match="INVALID_FIELD"):
        c.aggregate("BSEG", preds, ["NOPE"], [])
    with pytest.raises(rfc.RfcError, match="INVALID_FIELD"):
        c.aggregate("BSEG", preds, [], ["KOART"])  # not numeric
    with pytest.raises(rfc.RfcError, match="NOT_AUTHORIZED"):
        c.aggregate("KNA1", [], [], [])
    with pytest.raises(rfc.RfcError, match="INVALID_PREDICATE"):
        c.aggregate("BSEG", [{"FIELD": "BUKRS", "OP": "XX", "LOW": "1"}], [], [])
    r = addon.call(rfc.FM_AGGREGATE, IV_TABLE="BSEG", IT_PREDICATE=preds, IT_GROUP_BY=[{"FIELDNAME": "BUKRS"}], IT_SUM=[{"FIELDNAME": "DMBTR"}], IV_SNAPSHOT=c.snapshot)
    assert r["EV_CHECKSUM"] == rfc.package_checksum([e["JSON"] for e in r["ET_ROWS"]]) and r["EV_ROWS"] == 1
    with pytest.raises(rfc.RfcError, match="SNAPSHOT_UNKNOWN"):
        addon.call(rfc.FM_AGGREGATE, IV_TABLE="BSEG", IV_SNAPSHOT="nope")

    class Tampering:  # a transport that corrupts the aggregate on the wire
        name = "TAMPER"

        def call(self, fm, **p):
            out = addon.call(fm, **p)
            if fm == rfc.FM_AGGREGATE:
                out["ET_ROWS"][0]["JSON"] = out["ET_ROWS"][0]["JSON"].replace("COUNT", "C0UNT")
            return out

    t = rfc.AbapAddonClient(Tampering())
    with pytest.raises(rfc.RfcIntegrityError):
        t.aggregate("BSEG", preds, [], [])


# ------------------------------------------------------------------------------------- source view (RFC)
def test_source_view_reads_scope_through_the_addon_with_integrity_evidence(session, slice_result, monkeypatch):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    src = session.get(SapSystem, slice_result["source_id"])
    direct = RecordStore.load(session, src.id)
    scope = set(m.definition["company_codes"])
    rfc_src = SapSystem(id=src.id, sid=src.sid, client=src.client, role="SOURCE", product="ECC", release="6.0", connector="RFC", meta={"rfc": {"transport": "simulated"}})
    v = views.build_source_view(session, rfc_src, m)
    assert v.origin == "rfc_addon" and v.metrics["transport"] == "SIMULATED_ADDON" and v.metrics["aggregate_available"] is True and v.unreadable == set()
    for t in ("BKPF", "BSEG", "BSID", "BSIK", "ANLC", "T001K"):
        assert sorted(json.dumps(r, sort_keys=True) for r in v.rows(t)) == sorted(json.dumps(r, sort_keys=True) for r in direct.rows(t) if r["BUKRS"] in scope), t
    areas = {r["BWKEY"] for r in direct.rows("T001K") if r["BUKRS"] in scope}
    assert {r["BWKEY"] for r in v.rows("MBEW")} == areas and v.metrics["valuation_areas"] == sorted(areas)
    assert "KNA1" not in v.tables()  # only what the reconciliation reads
    integ = v.integrity_results("x")
    assert {r.check_name for r in integ} == {"source_read_integrity", "source_read_amounts"} and all(r.status == "PASS" for r in integ) and len([r for r in integ if r.check_name == "source_read_integrity"]) == 7
    # the view equals the direct read for the reconciliation: same outcome on the slice
    run = session.get(MigrationRun, slice_result["run_id"])
    tgt_view = views.build_target_view(session, session.get(SapSystem, slice_result["target_id"]), m, views.loaded_keys_of(get_backend(session=session), run.id), v)
    assert tgt_view.origin == "record_store"
    summ = reconcile_run(session, run, m, v, tgt_view)
    assert summ["overall"] == "PASS" and summ["views"]["source"]["origin"] == "rfc_addon" and summ["not_verified"] == []
    assert session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id, ReconciliationResult.check_name == "source_read_amounts")).scalars().first().status == "PASS"
    # a truncated read is caught by the aggregate evidence
    real_read_all = rfc.AbapAddonClient.read_all

    def truncated(self, table, preds):
        rows = list(real_read_all(self, table, preds))
        return iter(rows[:-1] if table == "BSEG" else rows)

    monkeypatch.setattr(rfc.AbapAddonClient, "read_all", truncated)
    v2 = views.build_source_view(session, rfc_src, m)
    bad = [r for r in v2.integrity_results("x") if r.status == "FAIL"]
    assert {r.check_name for r in bad} == {"source_read_integrity", "source_read_amounts"} and bad[0].subject == "BSEG" and "do not match" in bad[0].explanation
    monkeypatch.undo()
    # the row limit guards against reading a whole table
    with pytest.raises(views.ReconciliationViewError, match="SDTF_RECON_MAX_ROWS"):
        views.build_source_view(session, rfc_src, m, limit=10)
    # an add-on without Z_SDTF_AGGREGATE still serves rows, without evidence
    real_call = rfc.SimulatedAbapAddon.call

    def no_aggregate(self, fm, **p):
        if fm == rfc.FM_AGGREGATE:
            raise rfc.RfcError("FU_NOT_FOUND", "function module Z_SDTF_AGGREGATE does not exist")
        return real_call(self, fm, **p)

    monkeypatch.setattr(rfc.SimulatedAbapAddon, "call", no_aggregate)
    v3 = views.build_source_view(session, rfc_src, m)
    assert v3.metrics["aggregate_available"] is False and v3.integrity_results("x") == [] and v3.count("BSEG") == v.count("BSEG")
    # a table the user is not authorised for is reported, not fatal
    monkeypatch.undo()
    rfc_src.meta = {"rfc": {"transport": "simulated", "allowed_tables": ["T001K", "BKPF", "BSEG", "BSID", "BSIK", "MBEW"]}}
    v4 = views.build_source_view(session, rfc_src, m)
    assert v4.unreadable == {"ANLC"} and "ANLC" in v4.metrics["errors"]
    session.expire_all()


# ------------------------------------------------------------------------------------ target view (APIs)
def test_target_view_reads_back_through_the_apis(session, slice_result):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    run = session.get(MigrationRun, slice_result["run_id"])
    tgt = session.get(SapSystem, slice_result["target_id"])
    src = session.get(SapSystem, slice_result["source_id"])
    backend = get_backend(session=session)
    keys = views.loaded_keys_of(backend, run.id)
    direct = RecordStore.load(session, tgt.id)
    src_view = views.record_store_view(session, src.id)
    v = views.build_target_view(session, tgt, m, keys, src_view, force_api=True)
    assert v.origin == "api_readback" and v.metrics["transport"] in ("SIMULATED_S4",) and v.metrics["calls"] > 0
    assert set(views.UNREADABLE_BY_API) <= v.unreadable and "EKBE" in v.unreadable and not (v.unreadable & set(views._BOUND)) and v.derived_tables == {"BSID", "BSIK"}
    # company-code collections, keyed reads and the journal read service reproduce the record store
    cc_map = m.definition.get("target_ownership", {}).get("company_code_map") or {}
    tccs = {cc_map.get(c, c) for c in m.definition["company_codes"]}
    for t in ("KNB1", "LFB1", "EKKO"):
        assert {r for r in {views.record_key(t, x) for x in v.rows(t)}} == {views.record_key(t, x) for x in direct.rows(t) if x.get("BUKRS") in tccs}, t
    for t, ks in keys.items():
        if t in views._BOUND:
            for k in sorted(ks)[:50]:
                assert v.by_key(t, k) is not None, (t, k)
    tb = {views.record_key("BSEG", x) for x in direct.rows("BSEG") if x["BUKRS"] in tccs}
    assert {views.record_key("BSEG", x) for x in v.rows("BSEG")} == tb and len(tb) > 0
    assert {views.record_key("BKPF", x) for x in v.rows("BKPF")} == {views.record_key("BKPF", x) for x in direct.rows("BKPF") if x["BUKRS"] in tccs}
    l = v.rows("BSEG")[0]
    d = direct.by_key("BSEG", views.record_key("BSEG", l))
    assert float(l["DMBTR"]) == float(d["DMBTR"]) and l["SHKZG"] == d["SHKZG"] and l["HKONT"] == d["HKONT"]
    # open items are a projection of the journal lines on a real target (no clearing document); the synthetic BSID
    # table carries its own rows, so the view is compared with the lines, not with that table
    assert len(v.rows("BSID")) == len([x for x in direct.rows("BSEG") if x["BUKRS"] in tccs and x["KOART"] == "D" and not x.get("AUGBL")]) > 0
    assert len(v.rows("BSIK")) == len([x for x in direct.rows("BSEG") if x["BUKRS"] in tccs and x["KOART"] == "K" and not x.get("AUGBL")])
    # a master the load did not create is fetched lazily; an unknown key is cached as missing
    calls = v.client.stats()["calls"]
    assert v.by_key("KNA1", "nope") is None and v.by_key("KNA1", "nope") is None and v.client.stats()["calls"] == calls + 1
    # the full reconciliation through both views: identical verdict, unreadable tables reported, never a false FAIL
    summ = reconcile_run(session, run, m, src_view, v)
    assert summ["overall"] in ("PASS", "WARN") and summ["views"]["target"]["origin"] == "api_readback" and set(summ["not_verified"]) >= {"T001", "T001K", "ANLC"}
    rows = session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id)).scalars().all()
    assert not [r for r in rows if r.status == "FAIL"], [(r.check_name, r.subject, r.explanation) for r in rows if r.status == "FAIL"]
    warn = {(r.check_name, r.subject) for r in rows if r.status == "WARN" and r.evidence.get("unreadable")}
    assert ("asset_balances", "acquisition_values") in warn and ("inventory_valuation", "valuation_areas") in warn
    gl = [r for r in rows if r.check_name == "gl_balance"]
    assert gl and all(r.status == "PASS" for r in gl) and any(r.check_name == "trial_balance" and r.status == "PASS" for r in rows)
    session.expire_all()


class FakeS4Read:
    """A mocked S/4HANA that answers OData reads: keyed GETs, filtered collections with paging, journal items."""

    def __init__(self, store: RecordStore, ccs):
        self.store, self.ccs = store, set(ccs)
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET":
            return httpx.Response(405)
        if request.headers.get("x-csrf-token") == "fetch":
            return httpx.Response(200, json={"d": {}}, headers={"x-csrf-token": "t"})
        path = request.url.path.split("/sap/opu/odata/sap/", 1)[1]
        service, rest = path.split("/", 1)
        q = {k: v for k, v in request.url.params.items()}
        gw = self.gateway
        if service == views.JOURNAL_SERVICE:
            r = gw._journal_items("GET", rest + "?" + request.url.query.decode(), {})
        else:
            entity_set, key = tapi.parse_path(rest)
            b, eb = tapi._SERVICE_ENTITIES[service][entity_set]
            if key is None:
                r = gw._collection(eb, rest + "?" + request.url.query.decode())
            else:
                row = gw.row(eb.table, "|".join(gw._key_values(eb, key, None)))
                r = tapi.ApiResponse(404, {"error": {"code": "NOT_FOUND", "message": {"value": "x"}}}) if row is None else tapi.ApiResponse(200, {"d": eb.to_entity(row)})
        if "$top" in q and r.status == 200 and "results" in r.body.get("d", {}):
            assert int(q["$top"]) == views.PAGE
        return httpx.Response(r.status, json=r.body)


def test_target_view_over_http_with_paging(session, slice_result, monkeypatch):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    run = session.get(MigrationRun, slice_result["run_id"])
    tgt = session.get(SapSystem, slice_result["target_id"])
    backend = get_backend(session=session)
    keys = views.loaded_keys_of(backend, run.id)
    fake = FakeS4Read(RecordStore.load(session, tgt.id), m.definition["company_codes"])
    fake.gateway = tapi.SimulatedS4Gateway(session, tgt.id)
    monkeypatch.setattr(views, "PAGE", 7)  # exercise paging on the small landscape
    http_tgt = SapSystem(id=tgt.id, sid="S4H", client="100", role="TARGET", product="S4HANA", release="2023", connector="API", meta={"api": {"transport": "http", "dest": {"base_url": "https://s4.example.com", "user": "u", "passwd": "p"}}})
    real_make = tapi.make_target_transport

    def http_transport(sess, target):
        if target.sid == "S4H":
            return tapi.S4ApiHttpTransport(tapi.resolve_api_destination("S4H", target.meta), client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
        return real_make(sess, target)

    monkeypatch.setattr(views, "make_target_transport", http_transport)
    v = views.build_target_view(session, http_tgt, m, keys, views.record_store_view(session, session.get(SapSystem, slice_result["source_id"]).id))
    assert v.origin == "api_readback" and v.metrics["transport"] == "HTTP"
    direct = RecordStore.load(session, tgt.id)
    cc_map = m.definition.get("target_ownership", {}).get("company_code_map") or {}
    tccs = {cc_map.get(c, c) for c in m.definition["company_codes"]}
    assert {views.record_key("BSEG", x) for x in v.rows("BSEG")} == {views.record_key("BSEG", x) for x in direct.rows("BSEG") if x["BUKRS"] in tccs}
    assert {views.record_key("KNB1", x) for x in v.rows("KNB1")} == {views.record_key("KNB1", x) for x in direct.rows("KNB1") if x["BUKRS"] in tccs}
    paged = [r for r in fake.requests if "$skip=" in str(r.url) and "$skip=0" not in str(r.url)]
    assert paged, "collections were not paged"
    assert all(r.headers.get("authorization", "").startswith("Basic ") for r in fake.requests)
    by_service = defaultdict(int)
    for r in fake.requests:
        by_service[r.url.path.split("/sap/opu/odata/sap/", 1)[1].split("/")[0]] += 1
    assert by_service[views.JOURNAL_SERVICE] >= 1 and by_service["API_BUSINESS_PARTNER"] >= 1
    assert v.metrics["calls"] == len([r for r in fake.requests if r.headers.get("x-csrf-token") != "fetch"])
    # journal row derivation on its own
    rows = views.journal_rows([{"CompanyCode": "1710", "AccountingDocument": "1", "FiscalYear": "2026", "AccountingDocumentItem": "001", "FinancialAccountType": "D", "GLAccount": "121000", "AmountInCompanyCodeCurrency": "-50.5", "Customer": "C1", "TransactionCurrency": "EUR", "PostingDate": "20260101"}, {"CompanyCode": "1710", "AccountingDocument": "1", "FiscalYear": "2026", "LedgerGLLineItem": "002", "FinancialAccountType": "S", "GLAccount": "400000", "AmountInCompanyCodeCurrency": 50.5, "DebitCreditCode": "S"}])
    assert rows["BKPF"] == [{"BUKRS": "1710", "BELNR": "1", "GJAHR": "2026", "BUDAT": "20260101", "WAERS": "EUR"}]
    assert [(l["BUZEI"], l["SHKZG"], l["DMBTR"]) for l in rows["BSEG"]] == [("001", "H", 50.5), ("002", "S", 50.5)]
    assert rows["BSID"][0]["KUNNR"] == "C1" and rows["BSIK"] == []
    # $filter evaluator
    assert views.eval_filter("CompanyCode eq '1710' or CompanyCode eq '1010'", {"CompanyCode": "1010"}) and not views.eval_filter("A eq '1' and B eq '2'", {"A": "1", "B": "3"})
    with pytest.raises(ValueError):
        views.eval_filter("A gt 1", {})


# ---------------------------------------------------------------------------------------- API and CLI
def test_reconcile_again_api_and_cli(client, tokens, session, slice_result, capsys):
    run_id = slice_result["run_id"]
    assert client.post(f"{API}/runs/{run_id}/reconcile", headers=tokens["viewer"]).status_code == 403
    r = client.post(f"{API}/runs/{run_id}/reconcile", headers=tokens["operator"])
    assert r.status_code == 200, r.text
    summ = r.json()
    assert summ["overall"] == "PASS" and summ["reconciled_again_by"] == "operator" and summ["views"]["source"]["origin"] == "record_store"
    rep = client.get(f"{API}/runs/{run_id}/report", headers=tokens["viewer"]).json()
    assert rep["reconciliation"]["overall"] == "PASS"
    run = client.get(f"{API}/runs/{run_id}", headers=tokens["viewer"]).json()
    st = next(s for s in run["stages"] if s["name"] == "RECONCILE")
    assert st["status"] == "DONE" and st["metrics"]["reconciled_again_by"] == "operator"
    assert client.post(f"{API}/runs/nope/reconcile", headers=tokens["operator"]).status_code == 404
    assert any("Z_SDTF_AGGREGATE" in c["note"] for c in client.get(f"{API}/platform/capabilities", headers=tokens["viewer"]).json())
    # RFC connector test reports the aggregate module
    pid = slice_result["project_id"]
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "EC2", "client": "100", "role": "SOURCE", "product": "ECC", "release": "6.0", "connector": "RFC", "meta": {"rfc": {"transport": "simulated"}}}, headers=tokens["architect"])
    assert r.status_code == 201
    assert client.post(f"{API}/systems/{r.json()['id']}/import-synthetic", json={"scale": 1, "seed": 7}, headers=tokens["architect"]).status_code in (200, 201)
    t = client.post(f"{API}/systems/{r.json()['id']}/connector/test", headers=tokens["architect"]).json()
    assert t["ok"] and t["aggregate"]["available"] is True and t["aggregate"]["rows"] >= 0
    # CLI
    session.expire_all()
    capsys.readouterr()
    assert cli_main(["reconcile", "--run", run_id]) == 0
    out = capsys.readouterr().out
    assert "reconciliation PASS" in out and "source: record_store" in out and "target: record_store" in out
    assert cli_main(["reconcile", "--run", "nope"]) == 2


# ---------------------------------------------------------------------------- aggregate-only source view
def _financial(session, run_id):
    return {(r.check_name, r.subject): (r.status, r.source_value, r.target_value) for r in session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run_id, ReconciliationResult.layer == "FINANCIAL")).scalars().all()}


def test_aggregate_only_reconciliation_matches_the_row_read(session, slice_result, monkeypatch):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    run = session.get(MigrationRun, slice_result["run_id"])
    src = session.get(SapSystem, slice_result["source_id"])
    tgt = session.get(SapSystem, slice_result["target_id"])
    backend = get_backend(session=session)
    rfc_src = SapSystem(id=src.id, sid=src.sid, client=src.client, role="SOURCE", product="ECC", release="6.0", connector="RFC", meta={"rfc": {"transport": "simulated"}})
    rows_view = views.build_source_view(session, rfc_src, m, mode="rows")
    assert rows_view.metrics["mode"] == "rows" and rows_view.metrics["mode_decision"] == {"requested": "rows", "reason": "requested"}
    assert views.build_source_view(session, rfc_src, m).metrics["mode_decision"]["reason"].endswith(": rows")  # auto on the small landscape
    tgt_view = views.build_target_view(session, tgt, m, views.loaded_keys_of(backend, run.id), rows_view)
    session.query(ReconciliationResult).filter(ReconciliationResult.run_id == run.id).delete()
    by_rows = reconcile_run(session, run, m, rows_view, tgt_view)
    fin_rows = _financial(session, run.id)
    agg_view = views.build_source_view(session, rfc_src, m, mode="aggregate")
    assert isinstance(agg_view, views.AggregateSourceView) and agg_view.aggregate_only and agg_view.origin == "rfc_aggregate" and agg_view.metrics["mode"] == "aggregate" and agg_view.metrics["mode_decision"]["reason"] == "requested"
    # only T001K and the retained documents' lines crossed the wire; the large tables stayed in the source
    assert agg_view.rows("BSEG") == [] and agg_view.rows("BKPF") == [] and agg_view.count("T001K") > 0
    assert agg_view.metrics["rows"] == agg_view.count("T001K") + len(agg_view.retained_lines) and agg_view.metrics["rows_avoided"] == sum(agg_view.aggregates["counts"].values()) > agg_view.metrics["rows"]
    retained_all = {n.split(":", 1)[1] for n, c in m.selection["classification"].items() if c["type"] == "FI.AccountingDocument" and c["classification"] not in ("FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED")}
    retained_docs = {k for k in retained_all if k.split("|")[0] in set(m.definition["company_codes"])}  # the ParentCo side of cross-company documents is retained outside the scope: not read
    assert agg_view.metrics["retained_documents"] == len(retained_docs) and agg_view.metrics["retained_documents_outside_scope"] == len(retained_all) - len(retained_docs) and {f"{l['BUKRS']}|{l['BELNR']}|{l['GJAHR']}" for l in agg_view.retained_lines} == retained_docs
    integ = agg_view.integrity_results("x")
    assert {r.check_name for r in integ} == {"source_trial_balance"} and all(r.status == "PASS" for r in integ) and len(integ) == len(m.definition["company_codes"])
    session.query(ReconciliationResult).filter(ReconciliationResult.run_id == run.id).delete()
    by_agg = reconcile_run(session, run, m, agg_view, tgt_view)
    fin_agg = _financial(session, run.id)
    assert by_agg["overall"] == by_rows["overall"] == "PASS" and by_agg["views"]["source"]["mode"] == "aggregate"
    # the same financial verdicts and source totals, except currency totals which the aggregate source knows per company code only
    for key, (status, src_val, tgt_val) in fin_rows.items():
        if key[0] == "currency_totals":
            continue
        assert key in fin_agg, key
        assert fin_agg[key][0] == status and fin_agg[key][1] == src_val and fin_agg[key][2] == tgt_val, (key, fin_rows[key], fin_agg[key])
    cur_rows = {k: v for k, v in fin_rows.items() if k[0] == "currency_totals"}
    cur_agg = {k: v for k, v in fin_agg.items() if k[0] == "currency_totals"}
    assert cur_agg and all(k[1].endswith("/*") and v[0] == "PASS" for k, v in cur_agg.items()) and len(cur_agg) <= len(cur_rows)
    gl = [r for r in session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id, ReconciliationResult.check_name == "gl_balance")).scalars().all()]
    assert gl and all(r.evidence.get("source_mode") == "aggregate" for r in gl)
    assert any(r.check_name == "source_trial_balance" for r in session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id)).scalars().all())
    # auto mode decides by scope size
    monkeypatch.setenv("SDTF_RECON_AGGREGATE_ABOVE", "1")
    auto = views.build_source_view(session, rfc_src, m)
    assert auto.metrics["mode"] == "aggregate" and "above SDTF_RECON_AGGREGATE_ABOVE=1" in auto.metrics["mode_decision"]["reason"]
    monkeypatch.delenv("SDTF_RECON_AGGREGATE_ABOVE")
    monkeypatch.setenv("SDTF_RECON_MODE", "aggregate")
    assert views.build_source_view(session, rfc_src, m).metrics["mode"] == "aggregate"
    monkeypatch.delenv("SDTF_RECON_MODE")
    with pytest.raises(views.ReconciliationViewError, match="mode must be"):
        views.build_source_view(session, rfc_src, m, mode="totals")
    # without the aggregate module: auto falls back to rows, aggregate refuses
    real_call = rfc.SimulatedAbapAddon.call

    def no_aggregate(self, fm, **p):
        if fm == rfc.FM_AGGREGATE:
            raise rfc.RfcError("FU_NOT_FOUND", "function module Z_SDTF_AGGREGATE does not exist")
        return real_call(self, fm, **p)

    monkeypatch.setattr(rfc.SimulatedAbapAddon, "call", no_aggregate)
    fb = views.build_source_view(session, rfc_src, m)
    assert fb.metrics["mode"] == "rows" and fb.metrics["mode_decision"]["reason"].startswith("Z_SDTF_AGGREGATE unavailable")
    with pytest.raises(views.ReconciliationViewError, match="Z_SDTF_AGGREGATE"):
        views.build_source_view(session, rfc_src, m, mode="aggregate")
    monkeypatch.undo()
    session.expire_all()


def test_aggregate_mode_explains_retained_filtered_and_rejected_lines(session):
    """A scope with a fiscal-year window (lines never extracted) and excluded cross-company documents (retained in
    scope) plus an injected rejected line: the aggregate-only source explains every GL variance exactly like the
    row read, with the same verdicts, although no line item of the scope crossed the wire."""
    from sdtf.demo import approve_ruleset, create_demo_project, create_target_shell, demo_scope_definition
    from sdtf.discovery.service import discover_system
    from sdtf.graph.service import build_graph, persist_graph
    from sdtf.models import RuleSet, TransformationException
    from sdtf.rules.engine import parse_ruleset, validate_ruleset
    from sdtf.rules.factory import generate_candidate_ruleset
    from sdtf.runtime.pipeline import start_run
    from sdtf.scope.service import apply_disposition, approve_manifest, create_manifest, pending_dispositions

    ctx = create_demo_project(session, "architect", scale=1, seed=21, name="aggregate explain", connector="RFC")
    proj, src = ctx["project"], ctx["source"]
    store = RecordStore.load(session, src.id)
    discover_system(session, src, "architect", store)
    persist_graph(session, src.id, build_graph(store, src.id))
    tgt = create_target_shell(session, proj, "TAG", src)
    defn = demo_scope_definition(src, tgt, name="aggregate explain", fiscal_year_from=2024, fiscal_year_to=2025, historical_policy="YEARS", cross_company_policy="EXCLUDE")
    m = create_manifest(session, proj.id, defn, "architect")
    apply_disposition(session, m, pending_dispositions(m), "TRANSFER", "approver", "test")
    approve_manifest(session, m, "approver")
    y = generate_candidate_ruleset(defn, "S4HANA", name="aggregate-explain-rules")
    rs = parse_ruleset(y)
    row = RuleSet(project_id=proj.id, name=rs.name, version=1, content_hash=rs.content_hash, source_yaml=y, compiled={"rules": rs.rules}, validation=validate_ruleset(rs), created_by="architect")
    session.add(row)
    session.flush()
    approve_ruleset(session, row, "approver")
    run = start_run(session, proj.id, m.id, row.id, "operator")
    assert run.status == "COMPLETED" and src.connector == "RFC"
    scope = set(m.definition["company_codes"])
    retained = [n for n, c in m.selection["classification"].items() if c["type"] == "FI.AccountingDocument" and c["classification"] not in ("FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED") and n.split(":", 1)[1].split("|")[0] in scope]
    assert retained, "cross-company EXCLUDE retains documents inside the scope"
    backend = get_backend(session=session)
    # a line rejected by a rule: exception recorded, never loaded, absent from the target (as a real rejection leaves it)
    from sdtf.catalog.store import delete_records

    line = next(s for s in backend.iter_records(run.id, table="BSEG") if s.load_status == "LOADED")
    session.add(TransformationException(run_id=run.id, stage="TRANSFORM", table_name="BSEG", record_key=line.record_key, rule_id="test", severity="ERROR", message="injected"))
    line.load_status = "REJECTED"
    backend.update_records(run.id, [line])
    delete_records(session, tgt.id, "BSEG", [line.target_key])
    session.flush()

    def reconcile(mode):
        view = views.build_source_view(session, src, m, mode=mode)
        tgt_view = views.build_target_view(session, tgt, m, views.loaded_keys_of(backend, run.id), view)
        session.query(ReconciliationResult).filter(ReconciliationResult.run_id == run.id).delete()
        summ = reconcile_run(session, run, m, view, tgt_view)
        return view, summ, {(r.check_name, r.subject): r for r in session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id, ReconciliationResult.layer == "FINANCIAL")).scalars().all()}

    _rows_view, by_rows, fin_rows = reconcile("rows")
    agg_view, by_agg, fin_agg = reconcile("aggregate")
    assert agg_view.metrics["retained_documents"] == len(retained) and agg_view.retained_lines and agg_view.rows("BSEG") == []
    gl_rows = {k: v for k, v in fin_rows.items() if k[0] == "gl_balance"}
    gl_agg = {k: v for k, v in fin_agg.items() if k[0] == "gl_balance"}
    assert set(gl_rows) == set(gl_agg) and gl_rows
    warn = [k for k, v in gl_rows.items() if v.status == "WARN"]
    assert warn, "the fiscal-year window and the excluded cross-company documents leave explained variances"
    for k in gl_rows:
        assert (gl_agg[k].status, gl_agg[k].source_value, gl_agg[k].target_value, gl_agg[k].variance) == (gl_rows[k].status, gl_rows[k].source_value, gl_rows[k].target_value, gl_rows[k].variance), k
        if gl_rows[k].status != "PASS":
            assert ("unexplained 0.0" in gl_agg[k].explanation or "unexplained -0.0" in gl_agg[k].explanation) and "aggregate-only" in gl_agg[k].explanation, gl_agg[k].explanation
    assert not [k for k, v in gl_agg.items() if v.status == "FAIL"] and by_agg["overall"] == by_rows["overall"]
    expl = " ".join(gl_agg[k].explanation for k in warn)
    assert "not extracted" in expl and "retained/excluded by scope policy" in expl
    rejected_rows = [k for k in warn if "0.0 in lines rejected" not in gl_rows[k].explanation]
    assert rejected_rows and all("0.0 in lines rejected" not in gl_agg[k].explanation for k in rejected_rows)  # the rejected bucket, from the staging in aggregate mode
    for name in ("ar_open_items", "ap_open_items", "asset_balances", "inventory_valuation"):
        kr = next(k for k in fin_rows if k[0] == name)
        assert (fin_agg[kr].status, fin_agg[kr].source_value) == (fin_rows[kr].status, fin_rows[kr].source_value), name
    session.expire_all()
