"""API metadata verification: EDMX parsing (V2 and V4), expectations per service, deviations with suggestions,
the simulated gateway's generated metadata, a target check over a mocked HTTPS endpoint, the inventory guard
when the product valuation entity carries no stock value, API and CLI."""
import json

import httpx
import pytest

from sdtf.cli import main as cli_main
from sdtf.models import MigrationRun, SapSystem, ScopeManifest
from sdtf.reconciliation import views
from sdtf.runtime import metadata_check as mc
from sdtf.runtime import target_api as tapi
from sdtf.staging import get_backend

API = "/api/v1"

PRODUCT_EDMX_WITH_DEVIATIONS = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" xmlns:sap="http://www.sap.com/Protocols/SAPData">
<edmx:DataServices m:DataServiceVersion="2.0">
<Schema Namespace="API_PRODUCT_SRV" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
<EntityType Name="A_ProductType"><Key><PropertyRef Name="Product"/></Key><Property Name="Product" Type="Edm.String" Nullable="false" MaxLength="40"/><Property Name="ProductType" Type="Edm.String" MaxLength="4"/><Property Name="ProductGroup" Type="Edm.String" MaxLength="9"/><Property Name="BaseUnit" Type="Edm.String" MaxLength="3"/><Property Name="ProductDescription" Type="Edm.String"/></EntityType>
<EntityType Name="A_ProductPlantType"><Key><PropertyRef Name="Product"/><PropertyRef Name="Plant"/></Key><Property Name="Product" Type="Edm.String" Nullable="false"/><Property Name="Plant" Type="Edm.String" Nullable="false"/><Property Name="MRPResponsible" Type="Edm.String"/><Property Name="PurchasingGroup" Type="Edm.String"/><Property Name="ProcurementType" Type="Edm.String"/></EntityType>
<EntityType Name="A_ProductValuationType"><Key><PropertyRef Name="Product"/><PropertyRef Name="ValuationArea"/><PropertyRef Name="ValuationType"/></Key><Property Name="Product" Type="Edm.String" Nullable="false"/><Property Name="ValuationArea" Type="Edm.String" Nullable="false"/><Property Name="ValuationType" Type="Edm.String" Nullable="false"/><Property Name="ValuationClass" Type="Edm.String"/><Property Name="PriceDeterminationControl" Type="Edm.String" MaxLength="1"/><Property Name="StandardPrice" Type="Edm.Decimal" Precision="12" Scale="3"/><Property Name="MovingAveragePrice" Type="Edm.Decimal" Precision="12" Scale="3"/><Property Name="PriceUnitQty" Type="Edm.Decimal"/><Property Name="InventoryValuationProcedure" Type="Edm.String"/><Property Name="ValuationCategory" Type="Edm.String"/><Property Name="Currency" Type="Edm.String"/></EntityType>
<EntityContainer Name="API_PRODUCT_SRV_Entities" m:IsDefaultEntityContainer="true"><EntitySet Name="A_Product" EntityType="API_PRODUCT_SRV.A_ProductType"/><EntitySet Name="A_ProductPlant" EntityType="API_PRODUCT_SRV.A_ProductPlantType"/><EntitySet Name="A_ProductValuation" EntityType="API_PRODUCT_SRV.A_ProductValuationType"/></EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>"""

V4_EDMX = """<?xml version="1.0" encoding="utf-8"?><edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices><Schema Namespace="com.sap.gateway.srvd_a2x.api_fixedasset.v0001" xmlns="http://docs.oasis-open.org/odata/ns/edm"><EntityType Name="FixedAssetType"><Key><PropertyRef Name="CompanyCode"/><PropertyRef Name="MasterFixedAsset"/><PropertyRef Name="FixedAsset"/></Key><Property Name="CompanyCode" Type="Edm.String" Nullable="false"/><Property Name="MasterFixedAsset" Type="Edm.String" Nullable="false"/><Property Name="FixedAsset" Type="Edm.String" Nullable="false"/><Property Name="AssetClass" Type="Edm.String"/></EntityType><EntityContainer Name="EntityContainer"><EntitySet Name="FixedAsset" EntityType="com.sap.gateway.srvd_a2x.api_fixedasset.v0001.FixedAssetType"/></EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"""


def test_parse_edmx_v2_and_v4_and_detect_deviations():
    md = mc.parse_edmx(PRODUCT_EDMX_WITH_DEVIATIONS)
    assert md.version == "1.0" and md.entity_sets["A_ProductValuation"] == "A_ProductValuationType" and md.types["A_ProductValuationType"].keys == ["Product", "ValuationArea", "ValuationType"]
    assert md.types["A_ProductValuationType"].properties["PriceDeterminationControl"] == {"type": "Edm.String", "nullable": True, "max_length": "1"}
    res = mc.check_service(md, "API_PRODUCT_SRV")
    assert res["verdict"] == "ENTITY_SETS_MISSING" and res["entity_sets_missing"] == ["A_ProductStorageLocation"]  # MARD is not in this (deliberately partial) document
    val = next(e for e in res["entity_sets"] if e["entity_set"] == "A_ProductValuation")
    assert val["found"] and val["key_match"] and "PriceDeterminationControl" in val["properties_found"]
    assert set(val["properties_missing"]) == {"ValuationQuantity", "TotalValue"} and val["suggestions"]["ValuationQuantity"] == ["ValuationType"] or "ValuationArea" in val["suggestions"]["ValuationQuantity"]
    assert val["property_types"]["StandardPrice"]["type"] == "Edm.Decimal" and val["other_properties"] >= 4
    md4 = mc.parse_edmx(V4_EDMX)
    assert md4.version == "4.0" and md4.entity_sets == {"FixedAsset": "FixedAssetType"} and md4.types["FixedAssetType"].keys[0] == "CompanyCode"
    res4 = mc.check_service(md4, "API_FIXEDASSET")
    assert res4["verdict"] == "ENTITY_SETS_MISSING" and res4["entity_sets"][0]["closest_entity_sets"] == ["FixedAsset"]  # FixedAssetValuation is not there: the binding is a placeholder
    assert mc.check_service(md, "API_SALES_ORDER_SRV")["verdict"] == "ENTITY_SETS_MISSING" and mc.check_service(md, "NOPE_SRV")["verdict"] == "NOT_BOUND"
    with pytest.raises(ValueError, match="not an EDMX"):
        mc.parse_edmx("<html/>")
    with pytest.raises(ValueError):
        mc.parse_edmx("not xml")
    rep = mc.report_markdown(res)
    assert "## API_PRODUCT_SRV: ENTITY_SETS_MISSING" in rep and "missing `TotalValue`" in rep and "`A_ProductStorageLocation`" in rep


def test_expectations_cover_every_binding_and_the_simulator_meets_them():
    services = mc.bound_services()
    assert {"API_JOURNALENTRYITEMBASIC_SRV", "API_FIXEDASSET", "API_PRODUCT_SRV", "API_SALES_ORDER_SRV", "API_BUSINESS_PARTNER"} <= set(services)
    je = mc.expectations("API_JOURNALENTRYITEMBASIC_SRV")[0]
    assert je["entity_set"] == "A_JournalEntryItemBasic" and {"CompanyCode", "AccountingDocument", "FiscalYear", "GLAccount", "AmountInCompanyCodeCurrency", "DebitCreditCode"} <= set(je["properties"]) and "SpecialGLCode" in je["optional"]
    for s in services:
        res = mc.check_service(mc.parse_edmx(mc.edmx_from_bindings(s)), s)
        assert res["verdict"] == "VERIFIED", (s, res)
    gw = tapi.SimulatedS4Gateway(None, "x")
    res = mc.check_target(gw)
    assert res["catalog_available"] and res["summary"]["VERIFIED"] == len(services) and all(s["activated"] for s in res["services"])
    with pytest.raises(ValueError, match="not bound"):
        mc.edmx_from_bindings("NOPE_SRV")


class FakeS4Metadata:
    """A mocked S/4HANA that serves the catalogue and $metadata: the product service with the real-world
    deviations above, the journal service as bound, nothing else activated."""

    def __init__(self):
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not request.headers.get("authorization", "").startswith("Basic "):
            return httpx.Response(401)
        p = request.url.path
        if "catalogservice" in p:
            return httpx.Response(200, json={"d": {"results": [{"ID": "API_PRODUCT_SRV_0001", "TechnicalServiceName": "API_PRODUCT_SRV"}, {"ID": "API_JOURNALENTRYITEMBASIC_SRV_0001", "TechnicalServiceName": "API_JOURNALENTRYITEMBASIC_SRV"}]}})
        if p.endswith("/API_PRODUCT_SRV/$metadata"):
            return httpx.Response(200, text=PRODUCT_EDMX_WITH_DEVIATIONS, headers={"content-type": "application/xml"})
        if p.endswith("/API_JOURNALENTRYITEMBASIC_SRV/$metadata"):
            return httpx.Response(200, text=mc.edmx_from_bindings("API_JOURNALENTRYITEMBASIC_SRV"))
        if p.endswith("/$metadata"):
            return httpx.Response(404, text="<error>service not activated</error>")
        return httpx.Response(404)


def test_check_target_over_http(monkeypatch):
    fake = FakeS4Metadata()
    t = tapi.S4ApiHttpTransport({"base_url": "https://s4.example.com", "user": "u", "passwd": "p"}, client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    assert t.catalog() == ["API_JOURNALENTRYITEMBASIC_SRV", "API_PRODUCT_SRV"]
    res = mc.check_target(t)
    by = {s["service"]: s for s in res["services"]}
    assert res["catalog_available"] and by["API_PRODUCT_SRV"]["activated"] and by["API_PRODUCT_SRV"]["verdict"] == "ENTITY_SETS_MISSING"
    assert by["API_JOURNALENTRYITEMBASIC_SRV"]["verdict"] == "VERIFIED" and by["API_SALES_ORDER_SRV"]["verdict"] == "UNAVAILABLE" and by["API_SALES_ORDER_SRV"]["activated"] is False
    assert res["summary"]["VERIFIED"] == 1 and res["summary"]["UNAVAILABLE"] == len(res["services"]) - 2
    rep = mc.report_markdown(res)
    assert "## API_SALES_ORDER_SRV: UNAVAILABLE (not in the catalogue)" in rep and "## API_PRODUCT_SRV: ENTITY_SETS_MISSING (activated)" in rep


def test_inventory_not_readable_when_valuation_entity_has_no_stock_value(session, slice_result, monkeypatch):
    """A real product valuation entity may expose prices only: the view then reports MBEW as not readable and the
    inventory check says so instead of comparing against zero."""
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    run = session.get(MigrationRun, slice_result["run_id"])
    tgt = session.get(SapSystem, slice_result["target_id"])
    src = session.get(SapSystem, slice_result["source_id"])
    backend = get_backend(session=session)
    real_dispatch = tapi.SimulatedS4Gateway._dispatch

    def strip_total_value(self, method, service, path, payload, headers):
        r = real_dispatch(self, method, service, path, payload, headers)
        if service == "API_PRODUCT_SRV" and path.startswith("A_ProductValuation") and r.status == 200:
            for e in (r.body.get("d") or {}).get("results", []):
                e.pop("TotalValue", None)
        return r

    monkeypatch.setattr(tapi.SimulatedS4Gateway, "_dispatch", strip_total_value)
    sv = views.record_store_view(session, src.id)
    v = views.build_target_view(session, tgt, m, views.loaded_keys_of(backend, run.id), sv, force_api=True)
    assert "MBEW" in v.unreadable and "no stock value" in v.metrics["errors"]["MBEW"] and v.rows("MBEW")
    from sdtf.reconciliation.service import financial_checks, source_context

    results, _ = financial_checks(run.id, [source_context(m, sv, m.selection["classification"], [], [])], v)
    inv = next(r for r in results if r.check_name == "inventory_valuation")
    assert inv.status == "WARN" and inv.evidence.get("unreadable") and inv.target_value == "not readable"
    session.expire_all()


def test_metadata_api_and_cli(client, tokens, session, slice_result, tmp_path, capsys):
    r = client.post(f"{API}/metadata/check", json={"service": "API_PRODUCT_SRV", "content": PRODUCT_EDMX_WITH_DEVIATIONS}, headers=tokens["viewer"])
    assert r.status_code == 200 and r.json()["verdict"] == "ENTITY_SETS_MISSING" and "missing `TotalValue`" in r.json()["markdown"]
    assert client.post(f"{API}/metadata/check", json={"service": "API_PRODUCT_SRV", "content": "<html>not edmx</html>"}, headers=tokens["viewer"]).status_code == 422
    exp = client.get(f"{API}/metadata/expectations?service=API_JOURNALENTRYITEMBASIC_SRV", headers=tokens["viewer"]).json()
    assert list(exp["services"]) == ["API_JOURNALENTRYITEMBASIC_SRV"] and exp["services"]["API_JOURNALENTRYITEMBASIC_SRV"][0]["entity_set"] == "A_JournalEntryItemBasic"
    assert len(client.get(f"{API}/metadata/expectations", headers=tokens["viewer"]).json()["services"]) == len(mc.bound_services())
    src_id = slice_result["source_id"]
    assert client.post(f"{API}/systems/{src_id}/connector/metadata-check", headers=tokens["architect"]).status_code == 409  # not an API target
    pid = slice_result["project_id"]
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "S4M", "client": "100", "role": "TARGET", "product": "S4HANA", "release": "2023", "connector": "API", "meta": {"api": {"transport": "simulated"}}}, headers=tokens["architect"])
    assert r.status_code == 201
    sid = r.json()["id"]
    assert client.post(f"{API}/systems/{sid}/connector/metadata-check", headers=tokens["viewer"]).status_code == 403
    r = client.post(f"{API}/systems/{sid}/connector/metadata-check", headers=tokens["architect"])
    assert r.status_code == 200 and r.json()["transport"] == "SIMULATED_S4" and r.json()["summary"]["VERIFIED"] == len(mc.bound_services()) and r.json()["markdown"].startswith("# API metadata check")
    assert any(e["action"] == "METADATA_CHECKED" for e in client.get(f"{API}/audit/events?subject_id={sid}", headers=tokens["auditor"]).json())
    # CLI
    f = tmp_path / "API_PRODUCT_SRV.xml"
    f.write_text(PRODUCT_EDMX_WITH_DEVIATIONS, encoding="utf-8")
    capsys.readouterr()
    assert cli_main(["metadata", "check", "--file", str(f), "--service", "API_PRODUCT_SRV"]) == 0
    out = capsys.readouterr().out
    assert "API_PRODUCT_SRV: ENTITY_SETS_MISSING" in out and "PriceDeterminationControl" not in out.split("missing")[0] and "missing `TotalValue`" in out
    assert cli_main(["metadata", "check", "--file", str(f)]) == 2 and cli_main(["metadata", "check"]) == 2
    bad = tmp_path / "bad.xml"
    bad.write_text("<html/>", encoding="utf-8")
    assert cli_main(["metadata", "check", "--file", str(bad), "--service", "API_PRODUCT_SRV"]) == 2
    out_md = tmp_path / "report.md"
    session.expire_all()
    assert cli_main(["metadata", "check", "--system", sid, "--out", str(out_md), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["summary"]["VERIFIED"] == len(mc.bound_services()) and out_md.read_text().startswith("# API metadata check (SIMULATED_S4)")
    assert cli_main(["metadata", "check", "--system", "nope"]) == 2 and cli_main(["metadata", "expectations", "--service", "API_FIXEDASSET"]) == 0
    assert "FixedAssetValuation" in capsys.readouterr().out
    assert cli_main(["metadata", "expectations", "--json"]) == 0 and "A_SalesOrder" in capsys.readouterr().out
