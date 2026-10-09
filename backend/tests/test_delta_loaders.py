"""Delta loaders through released APIs: the simulated S/4HANA gateway as executable contract, the loader's
mapping of change sets to API operations, target-assigned numbering, reversals, blocks, unsupported objects,
the HTTP transport against a mocked server, and the API connector test."""
import json
import re

import httpx
import pytest
from sqlalchemy import select

from sdtf.catalog.api_bindings import API_BINDINGS, binding_for
from sdtf.catalog.store import RecordStore, import_tables
from sdtf.demo import create_demo_project, spinco_shell
from sdtf.models import DeltaEvent, SapSystem
from sdtf.runtime import target_api as tapi
from sdtf.runtime.loaders import DeltaLoader, EventView

API = "/api/v1"


@pytest.fixture
def target(session):
    """A prepared S/4HANA shell (SpinCo) with a few masters and one sales order, behind the simulated gateway."""
    ctx = create_demo_project(session, scale=1, seed=31, connector="RFC")
    tgt = ctx["target"]
    import_tables(session, tgt.id, {**spinco_shell(), "KNA1": [{"KUNNR": "100001", "NAME1": "Aurora Retail", "LAND1": "DE", "VBUND": "", "KTOKD": "KUNA"}], "MARA": [{"MATNR": "MAT-00001", "MTART": "FERT", "MATKL": "01", "MEINS": "PC", "MAKTX": "Widget"}], "VBAK": [{"VBELN": "7000001", "AUART": "OR", "VKORG": "SP01", "VTWEG": "10", "SPART": "00", "KUNNR": "100001", "AUDAT": "20260101", "WAERK": "EUR", "NETWR": 300.0, "BUKRS_VF": "SP01", "GBSTK": "A"}], "VBAP": [{"VBELN": "7000001", "POSNR": 10, "MATNR": "MAT-00001", "WERKS": "SP10", "KWMENG": 3, "NETWR": 300.0, "PRCTR": "P1"}]})
    gw = tapi.SimulatedS4Gateway(session, tgt.id)
    return tgt, gw, tapi.TargetApiClient(gw)


# ------------------------------------------------------------------------------------- gateway contract
def test_gateway_requires_csrf_and_validates_configuration(target):
    tgt, gw, _ = target
    r = gw.request("POST", "API_SALES_ORDER_SRV", "A_SalesOrder", {"SalesOrder": "7000002"})
    assert r.status == 403 and r.body["error"]["code"] == "CSRF"
    tok = gw.request("GET", "API_SALES_ORDER_SRV", "", None, {"x-csrf-token": "fetch"}).headers["x-csrf-token"]
    h = {"x-csrf-token": tok}
    assert gw.request("GET", "API_NOPE_SRV", "A_X", None, h).status == 404
    assert gw.request("POST", "API_SALES_ORDER_SRV", "A_Nope", {}, h).status == 404
    bad = gw.request("POST", "API_SALES_ORDER_SRV", "A_SalesOrder", {"SalesOrder": "7000002", "SalesOrganization": "9999", "SoldToParty": "100001"}, h)
    assert bad.status == 400 and "sales organization 9999" in bad.body["error"]["message"]["value"]
    bad = gw.request("POST", "API_SALES_ORDER_SRV", "A_SalesOrder", {"SalesOrder": "7000002", "SalesOrganization": "SP01", "to_Item": [{"SalesOrderItem": 10, "Material": "MAT-00001", "ProductionPlant": "ZZZZ", "RequestedQuantity": 1, "NetAmount": 10}]}, h)
    assert bad.status == 400 and "plant ZZZZ" in bad.body["error"]["message"]["value"]
    dup = gw.request("POST", "API_SALES_ORDER_SRV", "A_SalesOrder", {"SalesOrder": "7000001", "SalesOrganization": "SP01"}, h)
    assert dup.status == 409


def test_gateway_deep_insert_pricing_etags_and_delete(target):
    tgt, gw, c = target
    d = c.create("API_SALES_ORDER_SRV", "A_SalesOrder", {"SalesOrder": "7000002", "SalesOrderType": "OR", "SalesOrganization": "SP01", "DistributionChannel": "10", "OrganizationDivision": "00", "SoldToParty": "100001", "SalesOrderDate": "20260301", "TransactionCurrency": "EUR", "to_Item": [{"SalesOrderItem": 10, "Material": "MAT-00001", "ProductionPlant": "SP10", "RequestedQuantity": 2, "NetAmount": 50.0, "ProfitCenter": "P1"}, {"SalesOrderItem": 20, "Material": "MAT-00001", "ProductionPlant": "SP10", "RequestedQuantity": 1, "NetAmount": 25.0, "ProfitCenter": "P1"}]})
    assert d["SalesOrder"] == "7000002" and d["TotalNetAmount"] == 75.0 and d["OverallSDProcessStatus"] == "A" and d["BillingCompanyCode"] == "SP01"
    assert gw.row("VBAK", "7000002")["NETWR"] == 75.0 and len(gw.rows("VBAP")) == 3
    # quantity change re-prices the item at its unit price and the header total follows; derived props are read-only
    ent, etag = c.get("API_SALES_ORDER_SRV", "A_SalesOrderItem", ("SalesOrder", "SalesOrderItem"), ["7000002", "10"])
    assert ent["RequestedQuantity"] == 2 and etag
    c.update("API_SALES_ORDER_SRV", "A_SalesOrderItem", ("SalesOrder", "SalesOrderItem"), ["7000002", "10"], {"RequestedQuantity": 4}, etag)
    assert gw.row("VBAP", "7000002|10")["NETWR"] == 100.0 and gw.row("VBAK", "7000002")["NETWR"] == 125.0
    with pytest.raises(tapi.ApiError, match="412"):
        c.update("API_SALES_ORDER_SRV", "A_SalesOrderItem", ("SalesOrder", "SalesOrderItem"), ["7000002", "10"], {"RequestedQuantity": 5}, etag)  # stale ETag
    with pytest.raises(tapi.ApiError, match="READ_ONLY"):
        c.update("API_SALES_ORDER_SRV", "A_SalesOrder", ("SalesOrder",), ["7000002"], {"TotalNetAmount": 1.0}, None)
    with pytest.raises(tapi.ApiError, match="NOT_UPDATABLE"):
        c.update("API_SALES_ORDER_SRV", "A_SalesOrder", ("SalesOrder",), ["7000002"], {"SalesOrganization": "SP02"}, None)
    with pytest.raises(tapi.ApiError, match="KEY_IMMUTABLE"):
        c.update("API_SALES_ORDER_SRV", "A_SalesOrder", ("SalesOrder",), ["7000002"], {"SalesOrder": "1"}, None)
    _, etag = c.get("API_SALES_ORDER_SRV", "A_SalesOrderItem", ("SalesOrder", "SalesOrderItem"), ["7000002", "20"])
    c.delete("API_SALES_ORDER_SRV", "A_SalesOrderItem", ("SalesOrder", "SalesOrderItem"), ["7000002", "20"], etag)
    assert gw.row("VBAP", "7000002|20") is None and gw.row("VBAK", "7000002")["NETWR"] == 100.0
    _, etag = c.get("API_SALES_ORDER_SRV", "A_SalesOrder", ("SalesOrder",), ["7000002"])
    c.delete("API_SALES_ORDER_SRV", "A_SalesOrder", ("SalesOrder",), ["7000002"], etag)
    assert gw.row("VBAK", "7000002") is None and gw.row("VBAP", "7000002|10") is None  # cascade
    # masters cannot be deleted through the API
    with pytest.raises(tapi.ApiError, match="DELETE_NOT_ALLOWED"):
        c.delete("API_BUSINESS_PARTNER", "A_BusinessPartner", ("BusinessPartner",), ["100001"], None)
    assert c.stats()["failures"] == 5 and c.stats()["by_service"]["API_SALES_ORDER_SRV"] > 5
    # the gateway persisted everything: a fresh store sees the API's view
    fresh = RecordStore.load(session_of(gw), tgt.id, tables=["VBAK", "VBAP"])
    assert fresh.by_key("VBAK", "7000002") is None and fresh.by_key("VBAP", "7000001|10") is not None


def session_of(gw):
    return gw.session


def test_gateway_journal_entry_numbering_balance_and_reversal(target):
    tgt, gw, c = target
    lines = [{"ItemNumber": 1, "AccountType": "S", "DebitCreditCode": "S", "GLAccount": "400000", "CompanyCodeCurrencyAmount": 100.0, "TransactionCurrencyAmount": 100.0, "CostCenter": "SP0001"}, {"ItemNumber": 2, "AccountType": "D", "DebitCreditCode": "H", "GLAccount": "140000", "CompanyCodeCurrencyAmount": 100.0, "TransactionCurrencyAmount": 100.0, "Customer": "100001"}]
    hdr = {"CompanyCode": "SP01", "FiscalYear": 2026, "AccountingDocumentType": "SA", "DocumentDate": "20260301", "PostingDate": "20260301", "TransactionCurrency": "EUR", "DocumentReferenceID": "SIM-1", "AccountingDocument": "0600000001"}
    with pytest.raises(tapi.ApiError, match="does not balance"):
        c.post_journal_entry({**hdr, "Items": [lines[0]]})
    with pytest.raises(tapi.ApiError, match="company code ZZ99"):
        c.post_journal_entry({**hdr, "CompanyCode": "ZZ99", "Items": lines})
    d = c.post_journal_entry({**hdr, "Items": lines})
    assert d["numbering"] == "internal" and d["AccountingDocument"] != "0600000001" and d["AccountingDocument"].isdigit()
    doc = d["AccountingDocument"]
    assert gw.row("BKPF", f"SP01|{doc}|2026")["MONAT"] == 3 and len([l for l in gw.rows("BSEG") if l["BELNR"] == doc]) == 2
    assert any(r["BELNR"] == doc and r["KUNNR"] == "100001" for r in gw.rows("BSID"))  # open item derived from the customer line
    rev = c.reverse_journal_entry("SP01", doc, 2026)
    assert rev["ReversedDocument"] == doc and rev["AccountingDocument"] != doc
    net = sum(float(l["DMBTR"]) * (1 if l["SHKZG"] == "S" else -1) for l in gw.rows("BSEG") if l["HKONT"] == "400000" and l["BELNR"] in (doc, rev["AccountingDocument"]))
    assert abs(net) < 0.005
    with pytest.raises(tapi.ApiError, match="ALREADY_REVERSED"):
        c.reverse_journal_entry("SP01", doc, 2026)
    # external numbering keeps the proposed number
    gw2 = tapi.SimulatedS4Gateway(gw.session, tgt.id, numbering={"JournalEntry": "external"})
    d2 = tapi.TargetApiClient(gw2).post_journal_entry({**hdr, "AccountingDocument": "0600000009", "Items": lines})
    assert d2["AccountingDocument"] == "0600000009" and d2["numbering"] == "external"


def test_bindings_round_trip_rows_losslessly():
    for b in API_BINDINGS.values():
        for eb in [b.header, *b.items.values()]:
            from sdtf.catalog.tables import TABLES

            td = TABLES.get(eb.table)
            if td is None or not eb.fields:
                continue
            row = {f: f"v{i}" for i, f in enumerate(td.fields)}
            ent = eb.to_entity(row)
            back = eb.to_row(ent)
            assert {f: v for f, v in row.items() if f not in eb.derived} == back, (eb.table, set(row) ^ set(back))
            assert all(f not in eb.to_entity(row, create=False).values() for f in eb.priced)
            mapped, total = eb.coverage()
            assert mapped <= total
    assert binding_for("SD.BillingDocument") is None and binding_for("SD.SalesOrder").on_delete == "DELETE" and binding_for("FI.AccountingDocument").numbering == "internal"


# ------------------------------------------------------------------------------------------- the loader
def _ev(seq, table, op, payload, object_type, object_key, changenr="C1", target_key=None):
    from sdtf.catalog.tables import record_key

    return EventView(seq, table, op, record_key(table, payload) if payload else target_key or "", target_key or (record_key(table, payload) if payload else None), payload, object_type, object_key, changenr)


def test_loader_maps_change_sets_to_api_operations(target):
    tgt, gw, c = target
    loader = DeltaLoader(c, "S4HANA", read_row=gw.row)
    # new order with two items -> one deep insert
    hdr = {"VBELN": "7000003", "AUART": "OR", "VKORG": "SP01", "VTWEG": "10", "SPART": "00", "KUNNR": "100001", "AUDAT": "20260301", "WAERK": "EUR", "NETWR": 60.0, "BUKRS_VF": "SP01", "GBSTK": "A"}
    it1 = {"VBELN": "7000003", "POSNR": 10, "MATNR": "MAT-00001", "WERKS": "SP10", "KWMENG": 2, "NETWR": 40.0, "PRCTR": "P1"}
    it2 = {"VBELN": "7000003", "POSNR": 20, "MATNR": "MAT-00001", "WERKS": "SP10", "KWMENG": 1, "NETWR": 20.0, "PRCTR": "P1"}
    evs = [_ev(1, "VBAK", "I", hdr, "SD.SalesOrder", "7000003"), _ev(2, "VBAP", "I", it1, "SD.SalesOrder", "7000003"), _ev(3, "VBAP", "I", it2, "SD.SalesOrder", "7000003")]
    loader.load_change_set(evs)
    assert [e.result.action for e in evs] == ["INSERTED"] * 3 and "deep insert" in evs[0].result.api_call and evs[0].result.target_row["NETWR"] == 60.0
    assert c.stats()["by_operation"]["API_SALES_ORDER_SRV POST A_SalesOrder"] == 1
    # quantity change: item PATCH, header image is derived (no PATCH)
    evs = [_ev(4, "VBAP", "U", {**it1, "KWMENG": 3, "NETWR": 60.0}, "SD.SalesOrder", "7000003", "C2"), _ev(5, "VBAK", "U", {**hdr, "NETWR": 80.0}, "SD.SalesOrder", "7000003", "C2")]
    loader.load_change_set(evs)
    assert evs[0].result.action == "UPDATED" and "PATCH A_SalesOrderItem" in evs[0].result.api_call and evs[1].result.action == "DERIVED" and evs[1].result.target_row["NETWR"] == 80.0
    # a non-updatable header field -> rejected by the target with the property named
    evs = [_ev(6, "VBAK", "U", {**hdr, "NETWR": 80.0, "VKORG": "SP02"}, "SD.SalesOrder", "7000003", "C3")]
    loader.load_change_set(evs)
    assert evs[0].result.status == "REJECTED_BY_TARGET" and "SalesOrganization" in evs[0].result.message
    # item delete, header delete (cascade)
    evs = [_ev(7, "VBAP", "D", None, "SD.SalesOrder", "7000003", "C4", target_key="7000003|20")]
    loader.load_change_set(evs)
    assert evs[0].result.action == "DELETED" and gw.row("VBAP", "7000003|20") is None
    evs = [_ev(8, "VBAK", "D", None, "SD.SalesOrder", "7000003", "C5", target_key="7000003"), _ev(9, "VBAP", "D", None, "SD.SalesOrder", "7000003", "C5", target_key="7000003|10")]
    loader.load_change_set(evs)
    assert [e.result.action for e in evs] == ["DELETED", "DELETED"] and gw.row("VBAK", "7000003") is None
    # master delete -> blocked, not deleted
    evs = [_ev(10, "KNA1", "D", None, "MD.Customer", "100001", "C6", target_key="100001")]
    loader.load_change_set(evs)
    assert evs[0].result.action == "BLOCKED" and gw.row("KNA1", "100001")["IS_BLOCKED"] is True
    # journal entry: target assigns the number, lines and open items follow; update = reversal + repost; delete = reversal
    bk = {"BUKRS": "SP01", "BELNR": "0600000001", "GJAHR": 2026, "BLART": "SA", "BLDAT": "20260301", "BUDAT": "20260301", "MONAT": 3, "WAERS": "EUR", "AWTYP": "", "AWKEY": "", "BVORG": "", "XBLNR": "SIM", "BSTAT": ""}
    l1 = {"BUKRS": "SP01", "BELNR": "0600000001", "GJAHR": 2026, "BUZEI": 1, "KOART": "S", "SHKZG": "S", "HKONT": "400000", "DMBTR": 10.0, "WRBTR": 10.0, "KUNNR": "", "LIFNR": "", "KOSTL": "SP0001", "PRCTR": "", "AUGBL": "", "AUGDT": "", "VBUND": "", "MATNR": "", "WERKS": ""}
    l2 = {**l1, "BUZEI": 2, "SHKZG": "H", "HKONT": "800000"}
    evs = [_ev(11, "BKPF", "I", bk, "FI.AccountingDocument", "SP01|0600000001|2026", "C7"), _ev(12, "BSEG", "I", l1, "FI.AccountingDocument", "SP01|0600000001|2026", "C7"), _ev(13, "BSEG", "I", l2, "FI.AccountingDocument", "SP01|0600000001|2026", "C7")]
    loader.load_change_set(evs)
    assigned = evs[0].result.target_key
    assert evs[0].result.action == "INSERTED" and assigned != "SP01|0600000001|2026" and "target assigned document" in evs[0].result.message
    assert evs[1].result.target_key.startswith(assigned) and gw.row("BSEG", evs[1].result.target_key)["HKONT"] == "400000"
    evs = [_ev(14, "BKPF", "D", None, "FI.AccountingDocument", "SP01|0600000001|2026", "C8", target_key=assigned)]
    loader.load_change_set(evs)
    assert evs[0].result.action == "REVERSED" and gw.row("BKPF", evs[0].result.target_key)["AWTYP"] == "REVERSAL"
    # objects without API: cockpit-only, config, unbound, unknown
    evs = [_ev(15, "VBRK", "I", {"VBELN": "9", "FKART": "F2", "VKORG": "SP01", "KUNRG": "100001", "BUKRS": "SP01", "FKDAT": "20260301", "WAERK": "EUR", "NETWR": 1.0, "RFBSK": "", "GJAHR": 2026}, "SD.BillingDocument", "9", "C9")]
    loader.load_change_set(evs)
    assert evs[0].result.status == "UNSUPPORTED" and evs[0].result.load_method == "MIGRATION_COCKPIT" and "final delta" in evs[0].result.message
    evs = [_ev(16, "T001", "U", {"BUKRS": "SP01", "BUTXT": "x", "LAND1": "DE", "WAERS": "EUR", "KTOPL": "INT", "PERIV": "K4", "SPRAS": "E"}, "CFG.CompanyCode", "SP01", "C10"), _ev(17, "T001", "I", {"BUKRS": "SP09", "BUTXT": "x", "LAND1": "DE", "WAERS": "EUR", "KTOPL": "INT", "PERIV": "K4", "SPRAS": "E"}, "CFG.CompanyCode", "SP09", "C11")]
    loader.load_change_set(evs[:1])
    loader.load_change_set(evs[1:])
    assert evs[0].result.status == "MATCHED" and evs[1].result.status == "CONFIG_MISSING"
    evs = [_ev(18, "LIKP", "I", {"VBELN": "8", "LFART": "LF", "VSTEL": "SP10", "KUNNR": "100001", "WADAT_IST": "20260301", "WERKS": "SP10", "BUKRS": "SP01"}, "SD.Delivery", "8", "C12")]
    loader.load_change_set(evs)
    assert evs[0].result.status == "UNSUPPORTED" and "history" in evs[0].result.message


# ------------------------------------------------------------------------------------------ engine + API
def test_delta_cycle_reports_api_calls_and_assigned_numbers(session):
    from sdtf.demo import run_vertical_slice
    from sdtf.runtime.activity import simulate_business_activity
    from sdtf.runtime.delta import start_delta_cycle

    out = run_vertical_slice(session, scale=1, seed=12, connector="RFC")
    run, src, tgt = out["run"], out["source"], out["target"]
    assert tgt.connector == "API" and tgt.meta["api"]["transport"] == "simulated"
    simulate_business_activity(session, src, seed=4, count=15, company_codes=["5000"])
    c1 = start_delta_cycle(session, run.id, "operator")
    ap = next(st.metrics for st in c1.stages if st.name == "APPLY")
    assert ap["api"]["transport"] == "SIMULATED_S4" and ap["api"]["calls"] > 0 and ap["rejected_by_target"] == 0 and ap["by_load_method"].get("API", 0) > 0
    evs = session.execute(select(DeltaEvent).where(DeltaEvent.run_id == c1.id, DeltaEvent.status == "APPLIED")).scalars().all()
    assert all(e.load_method == "API" and e.api_call for e in evs)
    fi = [e for e in evs if e.table_name == "BKPF"]
    if fi:  # the target assigned the document numbers (which may coincide with the proposed ones); staging and ledger follow
        assert all(e.target_key.startswith("SP01|") and e.action == "INSERTED" and "JournalEntryBulkCreate" in e.api_call for e in fi)
        from sdtf.staging import get_backend

        staged = {(r.table_name, r.record_key): r for r in get_backend(session=session).iter_records(run.id, table="BKPF")}
        assert all(staged[("BKPF", e.record_key)].target_key == e.target_key for e in fi)
    rec = next(st.metrics for st in c1.stages if st.name == "RECONCILE")
    assert rec["overall"] == "PASS"


def test_api_connector_registration_and_test(client, tokens):
    arch = tokens["architect"]
    pid = client.post(f"{API}/projects", json={"name": "api-target", "scenario_type": "CARVE_OUT"}, headers=arch).json()["id"]
    assert client.get(f"{API}/catalog/adapters", headers=arch).json()["API"]["status"] == "IMPLEMENTED"
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "S4Q", "role": "SOURCE", "product": "S4HANA", "release": "2025", "connector": "API"}, headers=arch)
    assert r.status_code == 400 and "targets" in r.json()["detail"]
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "S4Q", "role": "TARGET", "product": "S4HANA", "release": "2025", "connector": "API", "meta": {"api": {"dest": {"base_url": "https://x", "passwd": "plain"}}}}, headers=arch)
    assert r.status_code == 400 and "never stored" in r.json()["detail"]
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "S4Q", "role": "TARGET", "product": "S4HANA", "release": "2025", "connector": "API", "meta": {"api": {"transport": "simulated"}}}, headers=arch)
    assert r.status_code == 201 and r.json()["connector_status"] == "SIMULATED"
    sid = r.json()["id"]
    t = client.post(f"{API}/systems/{sid}/connector/test", headers=arch).json()
    assert t["ok"] and t["transport"] == "SIMULATED_S4" and t["csrf_token"] and "API_SALES_ORDER_SRV" in t["services"] and t["numbering"]["JournalEntry"] == "internal"
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "S4R", "role": "TARGET", "product": "S4HANA", "release": "2025", "connector": "API"}, headers=arch)
    t = client.post(f"{API}/systems/{r.json()['id']}/connector/test", headers=arch).json()
    assert t["ok"] is False and t["error"] == "API_UNAVAILABLE" and "SDTF_S4_API_S4R" in t["detail"]


# ------------------------------------------------------------------------------------------ HTTP transport
class FakeS4:
    """A mocked S/4HANA endpoint: CSRF handshake, OData create/patch, SOAP journal entry."""

    def __init__(self):
        self.requests = []
        self.token = "tok-1"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Basic "):
            return httpx.Response(401, json={"error": {"code": "401", "message": {"value": "unauthorized"}}})
        if request.url.path.startswith("/sap/bc/srt/"):
            body = request.content.decode()
            if "<CompanyCode>SP01</CompanyCode>" not in body:
                return httpx.Response(500, text="<faultstring>company code missing</faultstring>")
            return httpx.Response(200, text="<n0:JournalEntryBulkCreateConfirmation><AccountingDocument>0100000042</AccountingDocument><FiscalYear>2026</FiscalYear></n0:JournalEntryBulkCreateConfirmation>")
        if request.method == "GET" and request.headers.get("x-csrf-token") == "fetch":
            return httpx.Response(200, json={"d": {}}, headers={"x-csrf-token": self.token})
        if request.method in ("POST", "PATCH", "DELETE") and request.headers.get("x-csrf-token") != self.token:
            return httpx.Response(403, json={"error": {"code": "CSRF", "message": {"value": "CSRF token validation failed"}}})
        if request.method == "POST":
            data = json.loads(request.content)
            return httpx.Response(201, json={"d": {**data, "TotalNetAmount": 0}}, headers={"etag": 'W/"1"'})
        if request.method == "PATCH":
            if request.headers.get("if-match") != 'W/"1"':
                return httpx.Response(412, json={"error": {"code": "412", "message": {"value": "precondition failed"}}})
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(200, json={"d": {"SalesOrder": "1"}}, headers={"etag": 'W/"1"'})
        return httpx.Response(405)


def test_http_transport_csrf_auth_odata_and_soap(monkeypatch):
    fake = FakeS4()
    monkeypatch.setenv("SDTF_S4_API_S4P", json.dumps({"base_url": "https://s4.example.com", "user": "SDTF_LOAD", "passwd": "env:S4_PW"}))
    monkeypatch.setenv("S4_PW", "secret")
    dest = tapi.resolve_api_destination("S4P")
    assert dest["passwd"] == "secret" and tapi.mask_api_destination(dest)["passwd"] == "***"
    t = tapi.S4ApiHttpTransport(dest, client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    c = tapi.TargetApiClient(t)
    d = c.create("API_SALES_ORDER_SRV", "A_SalesOrder", {"SalesOrder": "1", "SalesOrganization": "SP01"})
    assert d["SalesOrder"] == "1"
    paths = [(r.method, r.url.path, r.headers.get("x-csrf-token")) for r in fake.requests]
    assert paths[0] == ("GET", "/sap/opu/odata/sap/API_SALES_ORDER_SRV/", "fetch") and paths[-1][0] == "POST" and paths[-1][2] == "tok-1"
    ent, etag = c.get("API_SALES_ORDER_SRV", "A_SalesOrder", ("SalesOrder",), ["1"])
    assert ent["SalesOrder"] == "1" and etag == 'W/"1"'
    c.update("API_SALES_ORDER_SRV", "A_SalesOrder", ("SalesOrder",), ["1"], {"SalesOrderDate": "20260301"}, etag)
    assert fake.requests[-1].url.path.endswith("A_SalesOrder('1')") and fake.requests[-1].headers["if-match"] == 'W/"1"'
    with pytest.raises(tapi.ApiError, match="412"):
        c.update("API_SALES_ORDER_SRV", "A_SalesOrder", ("SalesOrder",), ["1"], {"SalesOrderDate": "20260302"}, 'W/"stale"')
    # CSRF token rotation: a 403 triggers one re-fetch
    fake.token = "tok-2"
    c.create("API_SALES_ORDER_SRV", "A_SalesOrder", {"SalesOrder": "2", "SalesOrganization": "SP01"})
    assert fake.requests[-1].headers["x-csrf-token"] == "tok-2"
    # SOAP journal entry
    d = c.post_journal_entry({"CompanyCode": "SP01", "FiscalYear": 2026, "DocumentDate": "20260301", "PostingDate": "20260301", "Items": [{"GLAccount": "400000", "CompanyCodeCurrencyAmount": 10.0, "DebitCreditCode": "S"}]})
    assert d["AccountingDocument"] == "0100000042" and d["FiscalYear"] == 2026
    env = fake.requests[-1].content.decode()
    assert re.search(r"<JournalEntryBulkCreateRequest>.*<CompanyCode>SP01</CompanyCode>.*<Item><GLAccount>400000</GLAccount>", env, re.S) and fake.requests[-1].headers["soapaction"] == "JournalEntryBulkCreateRequestConfirmation_In"
    with pytest.raises(tapi.ApiError, match="SOAP_FAULT"):
        c.post_journal_entry({"CompanyCode": "", "Items": []})
    with pytest.raises(tapi.ApiUnavailable, match="base_url"):
        tapi.S4ApiHttpTransport({"user": "x"})
    tgt = SapSystem(sid="S4Z", client="100", role="TARGET", product="S4HANA", release="2025", connector="API", meta={})
    with pytest.raises(tapi.ApiUnavailable, match="SDTF_S4_API_S4Z"):
        tapi.make_target_transport(None, tgt)
