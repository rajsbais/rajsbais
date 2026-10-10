"""Plant and sales-organisation scoping (ABAC) on top of systems and company codes."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from rfactory.api.main import create_app
from rfactory.security import authz
from rfactory.security.auth import DEMO_USERS as U, Forbidden, Principal
from rfactory.security.oidc import AuthConfig, AuthError, OidcVerifier
from rfactory.selective.manifest import Scope
from rfactory.sap.ddic import TABLES
from rfactory.service import Conflict

from .conftest import ADMIN, ALICE, CAROL

PETE = U["pete.plant"]


def project(svc, actor, scope, downstream=()):
    p = svc.create_project(actor, "scoped", svc.src_id, svc.tgt_id)
    svc.set_manifest(actor, p.id, scope, list(downstream), "gdpr-standard", {"DUPLICATE_DIFFERENT": "SKIP", "DUPLICATE_IDENTICAL": "SKIP"})
    return p


def single_site(svc, plant="1000", vkorg="1000"):
    """Make the source's sales data live entirely in one plant / sales organisation (the demo data mixes them)."""
    a = svc.adapters[svc.src_id]
    for t, f, v in (("VBAP", "WERKS", plant), ("LIPS", "WERKS", plant), ("LIKP", "WERKS", plant), ("VBAK", "VKORG", vkorg), ("KNVV", "VKORG", vkorg), ("MARC", "WERKS", plant)):
        for r in a.data[t]:
            r[f] = v
        a._invalidate(t)


def test_the_field_maps_name_real_columns():
    for m in (authz.PLANT_FIELDS, authz.SALES_ORG_FIELDS):
        for t, f in m.items():
            assert f in TABLES[t].fields, (t, f)


def test_attributes_make_a_principal_restricted():
    assert authz.restricted(PETE) and not authz.restricted(ALICE)
    assert authz.restricted(Principal("x", "x", ("basis",), attrs={"plants": []}))  # an empty list means nothing is allowed


def test_named_plants_and_sales_orgs_must_be_allowed(svc):
    with pytest.raises(Forbidden, match=r"plants \['1010'\]"):
        project(svc, PETE, Scope(object_type="MATERIAL", plants=["1000", "1010"]))
    with pytest.raises(Forbidden, match="sales orgs"):
        project(svc, PETE, Scope(object_type="CUSTOMER", sales_orgs=["2000"]))
    p = project(svc, PETE, Scope(object_type="MATERIAL", plants=["1000"]))
    assert p.manifest.scope.plants == ["1000"]


def test_a_material_plan_inside_the_scope_is_built(svc):
    p = project(svc, PETE, Scope(object_type="MATERIAL", plants=["1000"]))
    s = svc.build_plan(PETE, p.id)
    assert not s["blocking"]
    assert {r["WERKS"] for i in p.plan.instances.values() for r in i.rows.get("MARC", [])} == {"1000"}


def test_a_plan_that_contains_another_plant_is_refused_with_the_documents_named_and_nothing_is_stored(svc):
    p = project(svc, PETE, Scope(object_type="SALES_ORDER", plants=["1000"], date_from=svc.adapters[svc.src_id].reference_date() - timedelta(days=400),
                                 date_to=svc.adapters[svc.src_id].reference_date()))
    with pytest.raises(Forbidden) as e:
        svc.build_plan(PETE, p.id)
    assert "1010" in str(e.value) and __import__('re').search(r"[A-Z_]+:\d+", str(e.value)) and "narrow the scope" in str(e.value)
    assert p.plan is None  # the refused plan is never kept


def test_the_same_scope_passes_once_the_data_lives_in_the_allowed_plant_and_sales_org(svc):
    single_site(svc)
    ref = svc.adapters[svc.src_id].reference_date()
    p = project(svc, PETE, Scope(object_type="SALES_ORDER", plants=["1000"], date_from=ref - timedelta(days=400), date_to=ref))
    s = svc.build_plan(PETE, p.id)
    assert p.plan is not None and s["total_rows"] > 0


def test_a_foreign_sales_organisation_in_the_closure_is_refused(svc):
    single_site(svc)
    a = svc.adapters[svc.src_id]
    a.data["VBAK"][0]["VKORG"] = "2000"
    a._invalidate("VBAK")
    ref = a.reference_date()
    p = project(svc, PETE, Scope(object_type="SALES_ORDER", plants=["1000"], date_from=ref - timedelta(days=400), date_to=ref))
    with pytest.raises(Forbidden, match=r"sales organisation\(s\) \['2000'\]"):
        svc.build_plan(PETE, p.id)


def test_an_unrestricted_principal_and_a_company_only_principal_are_unaffected(svc):
    p = project(svc, ALICE, Scope(object_type="MATERIAL", plants=["1000", "1010"]))
    svc.build_plan(ALICE, p.id)
    cora = U["cora.regional"]
    q = svc.create_project(cora, "c", svc.src_id, svc.tgt_id)
    svc.set_manifest(cora, q.id, Scope(object_type="MATERIAL", company_codes=["2000"], plants=["2000"]), [], "gdpr-standard", {})  # plants are not restricted for her


def test_delta_scopes_are_dry_planned_for_a_plant_restricted_principal(svc):
    from .test_delta import spec
    ok = {"scope": Scope(object_type="MATERIAL", plants=["1000"])}
    sc = svc.delta.create(PETE, spec(svc, scopes=[ok], include_downstream=[], schedule=None))
    assert sc.id
    with pytest.raises(Forbidden, match="1010"):  # sales orders that touch plant 1010
        svc.delta.create(PETE, spec(svc, scopes=[{"scope": Scope(object_type="SALES_ORDER", plants=["1000"]), "rolling_days": 400}], include_downstream=[], schedule=None))
    with pytest.raises(Forbidden, match="plants"):
        svc.delta.create(PETE, spec(svc, scopes=[{"scope": Scope(object_type="MATERIAL", plants=["1010"])}], include_downstream=[], schedule=None))
    with pytest.raises(Forbidden):
        sc2 = svc.delta.create(ALICE, spec(svc, scopes=[ok], include_downstream=[], schedule=None))  # change by the restricted user must also be checked
        svc.delta.update(PETE, sc2.id, spec(svc, scopes=[{"scope": Scope(object_type="MATERIAL", plants=["1010"])}], include_downstream=[], schedule=None))


def test_discovery_lists_only_what_the_principal_may_select(tmp_path):
    c = TestClient(create_app(tmp_path))
    H = lambda u: {"X-Demo-User": u}
    b = c.post("/api/demo/bootstrap", headers=H("root.admin")).json()
    sid = b["source"]["id"]
    full = c.get(f"/api/systems/{sid}/discovery", headers=H("alice.basis")).json()
    assert {p["plant"] for p in full["plants"]} == {"1000", "1010", "2000"} and len(full["company_codes"]) == 2
    pete = c.get(f"/api/systems/{sid}/discovery", headers=H("pete.plant")).json()
    assert {p["plant"] for p in pete["plants"]} == {"1000"} and len(pete["company_codes"]) == 2  # companies are not restricted for him
    cora = c.get(f"/api/systems/{sid}/discovery", headers=H("cora.regional")).json()
    assert [x["code"] for x in cora["company_codes"]] == ["2000"] and {p["plant"] for p in cora["plants"]} == {"2000"}
    me = c.get("/api/me", headers=H("pete.plant")).json()
    assert me["scope"]["plants"] == ["1000"] and me["scope"]["sales_orgs"] == ["1000"]


def test_oidc_claims_carry_the_new_attributes():
    v = OidcVerifier(AuthConfig(mode="oidc", issuer="i", audience="a", jwks_file="x"), jwks={"keys": []})
    p = v.principal({"sub": "u", "roles": ["data_steward"], "rf_plants": ["1000"], "rf_sales_orgs": ["1000", "1010"]})
    assert p.attrs == {"plants": ["1000"], "sales_orgs": ["1000", "1010"]}
    with pytest.raises(AuthError):
        v.principal({"sub": "u", "rf_plants": "1000"})
    with pytest.raises(AuthError):
        v.principal({"sub": "u", "rf_sales_orgs": [1000]})


def test_a_configuration_reference_to_a_foreign_plant_is_refused_even_without_rows():
    plan = type("Plan", (), {"instances": {}, "config_refs": {"PLANT": {"1010"}, "COMPANY_CODE": set()}})()
    with pytest.raises(Forbidden, match=r"plant\(s\) \['1010'\]"):
        authz.require_plan(PETE, plan, {})
    plan.config_refs["PLANT"] = {"1000"}
    authz.require_plan(PETE, plan, {})
