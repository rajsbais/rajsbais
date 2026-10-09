"""Remote read-only connectors (RFC adapter against a FAKE RFC transport). Nothing here touches a real SAP system."""
import json
from datetime import date, datetime, timezone

import pytest

from rfactory.sap.adapter import ChangeLogGap, ReadOnlyView, SapSystem
from rfactory.sap.connectors import rfc as R
from rfactory.sap.connectors.contract import norm, normset, source_contract
from rfactory.sap.connectors.fake_rfc import FakeRfcTransport
from rfactory.sap.connectors.profile import ConnectionProfile, ProfileError, resolve_secret
from rfactory.sap.synthetic import make_demo_pair
from rfactory.security.auth import DEMO_USERS as U, Forbidden
from rfactory.service import Conflict, RefreshService

from .conftest import ADMIN, ALICE, CAROL, make_project, ready_project

NO_SLEEP = lambda s: None
COPILOT = U["refresh.copilot"]


def mk(family="ECC", transport_kw=None, profile_kw=None, **kw):
    sim, _ = make_demo_pair(family)
    tr = FakeRfcTransport(sim, **(transport_kw or {}))
    prof = ConnectionProfile("t", "rfc", ashost="h", client="100", calls_per_minute=10 ** 6, **(profile_kw or {}))
    return sim, tr, R.RfcSourceAdapter(sim.system, tr, prof, sleep=NO_SLEEP, reference=lambda: sim.reference_date(), **kw)


# ---------------------------------------------------------------- contract
@pytest.mark.parametrize("family", ["ECC", "S4"])
def test_rfc_adapter_conforms_to_the_source_contract(family):
    sim, tr, a = mk(family)
    assert source_contract(a, sim) == []
    assert a.schema_drift() == {}


def test_the_simulator_itself_satisfies_the_contract_and_the_contract_catches_a_bad_adapter():
    sim, _ = make_demo_pair()
    assert source_contract(sim, sim) == [f"a read-only source adapter exposes {n}()" for n in ("upsert", "delete", "set_number_level", "sim_insert")
                                         if hasattr(sim, n)]
    _, _, a = mk()

    class Lossy:
        def __init__(self, inner): self.i = inner
        def __getattr__(self, n): return getattr(self.i, n)
        def select(self, t, p=None): return self.i.select(t, p)[:-1]
    s2, _ = make_demo_pair()
    assert any("select() differs" in v for v in source_contract(Lossy(a), s2, tables=["VBAK"]))


# ---------------------------------------------------------------- RFC_READ_TABLE's constraints
def test_wide_tables_are_read_in_field_groups_that_repeat_the_key():
    sim, tr, a = mk(transport_kw={"widen": {"KNA1": {"NAME1": 300, "STRAS": 300}}})
    rows = a.select("KNA1")
    assert normset(rows) == normset(sim.select("KNA1"))
    reads = [c for c in tr.calls if c["function"] == "RFC_READ_TABLE" and c["QUERY_TABLE"] == "KNA1"]
    assert len({tuple(c["fields"]) for c in reads}) >= 2 and all("KUNNR" in c["fields"] for c in reads)  # several groups, key in each
    meta = a.meta("KNA1")
    assert all(sum(meta.width(f) for f in c["fields"]) <= 512 for c in reads)  # no call exceeded the 512-character row


def test_a_field_that_cannot_fit_beside_the_key_is_reported_not_truncated():
    _, _, a = mk(transport_kw={"widen": {"KNA1": {"NAME1": 600}}})
    with pytest.raises(R.RemoteError, match="cannot be read together with the key"):
        a.select("KNA1")


def test_where_clauses_split_on_token_boundaries_within_72_characters():
    toks = ["MATNR", "IN", "("] + [f"'{i:018d}'," for i in range(30)] + [")"]
    lines = R.wrap_lines(toks)
    assert all(len(l["TEXT"]) <= 72 and l["TEXT"].endswith(" ") for l in lines)
    assert " ".join(t for l in lines for t in l["TEXT"].split()) == " ".join(toks)  # no token was cut or lost
    assert len(lines) > 1
    with pytest.raises(R.RemoteError):
        R.wrap_lines(["'" + "x" * 80 + "'"])


def test_long_value_lists_and_awkward_literals_survive_the_wire():
    sim, tr, a = mk()
    sim.data["KNA1"][0]["NAME1"] = "O'Brien & Sons (Ltd)"
    sim._invalidate("KNA1")
    assert [r["KUNNR"] for r in a.lookup("KNA1", "NAME1", "O'Brien & Sons (Ltd)")] == [sim.data["KNA1"][0]["KUNNR"]]
    keys = sorted({r["VBELN"] for r in sim.data["VBAK"]})
    got = a.select_in("VBAK", "VBELN", keys)  # 55 values: one clause, many lines
    assert len(got) == len(keys)
    many = keys * 5 + [f"{i:010d}" for i in range(300)]  # > 100 values: batched
    assert len(a.select_in("VBAK", "VBELN", many)) == len(keys)
    assert max(len("".join(o["TEXT"] for o in c.get("OPTIONS", []))) for c in tr.calls if c["function"] == "RFC_READ_TABLE") > 72  # really multi-line
    assert all(len(o["TEXT"]) <= 72 for c in tr.calls for o in c.get("OPTIONS", []))


def test_dates_numbers_and_signs_round_trip():
    sim, _, a = mk()
    sim.data["BSEG"][0]["DMBTR"] = -1234.5
    sim._invalidate("BSEG")
    k = tuple(sim.data["BSEG"][0][f] for f in ("BUKRS", "BELNR", "GJAHR", "BUZEI"))
    assert a.get("BSEG", k)["DMBTR"] == -1234.5
    d = a.select_between("VBAK", "ERDAT", "2026-07-01", "2026-09-01")
    assert d and all("2026-07-01" <= r["ERDAT"] <= "2026-09-01" for r in d) and isinstance(d[0]["NETWR"], float)
    assert isinstance(a.get("VBAP", ("0005000001", "000010"))["KWMENG"], int)


def test_paging_is_complete_and_unstable_paging_is_detected():
    sim, tr, a = mk(profile_kw={"page_rows": 7})
    assert normset(a.select("VBAK")) == normset(sim.select("VBAK"))
    assert sum(1 for c in tr.calls if c["function"] == "RFC_READ_TABLE" and c["QUERY_TABLE"] == "VBAK") >= 8  # 55 rows / 7 per page
    sim2, tr2, b = mk(transport_kw={"unstable": {"VBAK"}}, profile_kw={"page_rows": 7})
    with pytest.raises(R.UnstablePaging, match="quiesced"):
        b.select("VBAK")


def test_a_source_that_changes_between_field_groups_is_refused():
    sim, tr, a = mk(transport_kw={"widen": {"KNA1": {"NAME1": 300, "STRAS": 300}}})
    real = tr.call
    state = {"n": 0}

    def hook(function, **p):
        r = real(function, **p)
        if function == "RFC_READ_TABLE" and p["QUERY_TABLE"] == "KNA1":
            state["n"] += 1
            if state["n"] == 1:  # a row disappears after the first group was read
                sim.data["KNA1"] = sim.data["KNA1"][1:]
        return r
    tr.call = hook
    with pytest.raises(R.SourceChangedDuringRead):
        a.select("KNA1")


# ---------------------------------------------------------------- safety
def test_full_scans_are_bounded_and_pushdown_is_not():
    sim, tr, a = mk(profile_kw={"max_scan_rows": 10})
    with pytest.raises(R.ScanTooLarge, match="narrow the selection"):
        a.select("VBAK")
    assert a.stats.guard_trips == 1
    assert len(a.lookup("VBAK", "VBELN", "0005000001")) <= 1  # narrow questions still work
    counts = a.table_counts()  # never raises: capped values, flagged by guard_trips
    assert counts["VBAK"] == 10 and a.stats.guard_trips >= 2


def test_only_allow_listed_function_modules_can_be_called_and_nothing_can_write():
    sim, tr, a = mk()
    for fm in ("BAPI_SALESORDER_CREATEFROMDAT2", "RFC_ABAP_INSTALL_AND_RUN", "SXPG_COMMAND_EXECUTE", "BAPI_TRANSACTION_COMMIT"):
        with pytest.raises(R.FunctionNotAllowed):
            a._t.call(fm)
    assert not tr.calls or all(c["function"] in R.ALLOWED_FUNCTIONS for c in tr.calls)
    view = ReadOnlyView(a)
    for n in ("upsert", "delete", "set_number_level", "_t"):
        with pytest.raises(AttributeError):
            getattr(view, n)
    assert a.capabilities()["writes"] is False and a.capabilities()["validated_against_real_sap"] is False


def test_errors_are_mapped_and_transient_ones_retried_with_backoff():
    sim, tr, a = mk(transport_kw={"denied": {"KNB1"}})
    with pytest.raises(R.RemoteAuthError):
        a.select("KNB1")
    with pytest.raises(R.RemoteTableMissing):
        a.meta("ZNOPE")
    sleeps = []
    sim2, tr2, b = mk()
    b._sleep = sleeps.append
    tr2.fail_next(2)
    assert b.get("VBAK", ("0005000001",)) is not None and b.stats.retries == 2 and 0.5 in sleeps and 1.0 in sleeps
    tr2.fail_next(3)
    with pytest.raises(R.RfcCommunicationError, match="3 attempts"):
        b.select("T001")


def test_calls_are_throttled():
    sim, _ = make_demo_pair()
    tr, t = FakeRfcTransport(sim), [0.0]
    sleeps = []
    a = R.RfcSourceAdapter(sim.system, tr, ConnectionProfile("t", "rfc", ashost="h", client="100", calls_per_minute=60), sleep=sleeps.append, clock=lambda: t[0])
    a.get("T001", ("1000",))
    a.get("T001", ("2000",))
    assert any(abs(s - 1.0) < 0.01 for s in sleeps)  # 60 calls/minute = one call per second


def test_schema_drift_against_the_model_is_reported():
    sim, tr, a = mk()
    tr._dfies_raw("VBAK")
    tr._meta_cache["VBAK"] = [d for d in tr._meta_cache["VBAK"] if d["FIELDNAME"] != "ERNAM"]
    tr._meta_cache["KNA1"] = [{**d, "KEYFLAG": ""} if d["FIELDNAME"] == "KUNNR" else d for d in tr._dfies_raw("KNA1")] + \
                             [{"FIELDNAME": "MANDT", "POSITION": "0000", "KEYFLAG": "X", "DATATYPE": "CLNT", "LENG": "3", "DECIMALS": "0", "INTTYPE": "C"}]
    drift = a.schema_drift(["VBAK", "KNA1"])
    assert any("ERNAM" in x for x in drift["VBAK"]) and any("key" in x for x in drift["KNA1"])


def test_profiles_never_hold_secrets(monkeypatch, tmp_path):
    with pytest.raises(ProfileError, match="env:NAME or file"):
        ConnectionProfile("p", "rfc", ashost="h", client="100", password_ref="hunter2").validate()
    monkeypatch.setenv("SAP_PW", "s3cret")
    assert resolve_secret("env:SAP_PW") == "s3cret"
    f = tmp_path / "pw"
    f.write_text("fromfile\n")
    assert resolve_secret(f"file:{f}") == "fromfile"
    with pytest.raises(ProfileError):
        resolve_secret("env:NOT_SET_ANYWHERE")
    p = ConnectionProfile("p", "rfc", ashost="h", client="100", user="U", password_ref="env:SAP_PW")
    assert "SAP_PW" not in json.dumps(p.public()) and "s3cret" not in json.dumps(p.public()) and p.public()["password_ref"] == "env:***"
    for bad in (ConnectionProfile("p", "rfc"), ConnectionProfile("p", "odata", base_url="http://evil.example"), ConnectionProfile("p", "ftp"),
                ConnectionProfile("p", "rfc", ashost="h", client="1", page_rows=0)):
        with pytest.raises(ProfileError):
            bad.validate()


# ---------------------------------------------------------------- platform integration
def remote_svc(tmp_path, family="ECC", persist=False):
    svc = RefreshService(tmp_path, persist=persist)
    b = svc.bootstrap_demo(ADMIN)
    svc.src_id, svc.tgt_id, svc.s4_src, svc.s4_tgt = b["source"]["id"], b["target"]["id"], b["s4_source"]["id"], b["s4_target"]["id"]
    sim, _ = make_demo_pair(family)
    sys_ = SapSystem(sid="EP2" if family == "ECC" else "S4X", client="100", role="PRD", product=sim.system.product, release=sim.system.release,
                     db_type=sim.system.db_type, owner="ops")
    prof = ConnectionProfile("remote", "rfc", ashost="h", client="100", user="U", password_ref="env:X", calls_per_minute=10 ** 6)
    svc.connect_remote(ADMIN, sys_, prof, transport=FakeRfcTransport(sim), reference=sim.reference_date)  # a real system reports today's date
    svc.adapters[sys_.id]._sleep = NO_SLEEP
    return svc, sys_.id, sim


def test_connecting_a_remote_source_is_governed_and_it_can_never_be_a_target(tmp_path):
    svc, rid, _ = remote_svc(tmp_path)
    s = svc.system(rid)
    assert not s.can_be_write_target and s.adapter == "rfc" and not svc.is_local(rid)
    assert any(e["action"] == "system.connected" for e in svc.audit.entries()) and "env:X" not in json.dumps(svc.audit.entries()[-1]["details"]["profile"]["password_ref"])
    sys2 = SapSystem(sid="EPX", client="100", role="PRD")
    prof = ConnectionProfile("x", "rfc", ashost="h", client="100")
    for who in (ALICE.__class__("t", "t", ("tester",)), COPILOT, U["svc.scheduler"]):
        with pytest.raises(Forbidden):
            svc.connect_remote(who, sys2, prof, transport=FakeRfcTransport(make_demo_pair()[0]))
    with pytest.raises(Conflict, match="needs user and password_ref"):
        svc.connect_remote(ADMIN, sys2, ConnectionProfile("o", "odata", base_url="https://x.example"))  # no credentials: refused before any network call
    with pytest.raises(Conflict, match="pyrfc"):
        svc.connect_remote(ADMIN, sys2, prof)  # no transport given: the real one needs pyrfc + the SAP SDK
    with pytest.raises(Forbidden, match="production systems cannot be selected as refresh targets"):
        svc.create_project(ALICE, "x", svc.src_id, rid)  # a remote system as TARGET is refused
    nonprod = SapSystem(sid="EQ9", client="100", role="QAS", owner="o")
    svc.connect_remote(ADMIN, nonprod, prof, transport=FakeRfcTransport(make_demo_pair()[1]))
    with pytest.raises(Forbidden, match="locked"):
        svc.create_project(ALICE, "x", svc.src_id, nonprod.id)  # even a non-production remote system is read-only


def test_remote_system_features_are_scoped_to_what_is_safe(tmp_path):
    svc, rid, _ = remote_svc(tmp_path)
    assert svc.discover(rid)["business_objects"] is None
    r = svc.readiness(rid)
    assert r["ready"] and r["checks"][0]["name"].startswith("Remote read-only") and not r["simulated"]
    with pytest.raises(Conflict, match="remote read-only system"):
        svc.postcopy.assess(rid)
    with pytest.raises(Conflict, match="remote read-only system"):
        svc.full.create(ALICE, {"source_id": rid, "target_id": svc.tgt_id, "profile_id": "x", "backup_ref": "b"})
    rep = svc.agents.run(ALICE, "landscape-discovery", {})
    assert any("remote read-only source" in f["text"] for f in rep["findings"])
    assert svc.agents.run(ALICE, "compliance-verification", {})["artifacts"]["controls"]


def test_a_selective_refresh_from_a_remote_source_equals_one_from_the_simulator(tmp_path, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("RF_MASKING_KEY", "fixed-key-for-comparison")
    svc, rid, _ = remote_svc(tmp_path)
    p = ready_project(svc, src=rid, tgt=svc.tgt_id)
    assert not p.plan.blocking and p.plan.summary()["total_rows"] > 100
    scans_before = svc.adapters[rid].stats.scans
    summary = svc.plan_summary(p.id)
    assert summary["source_total_rows"] is None  # not read from a remote system
    svc.submit(ALICE, p.id)
    svc.approve(CAROL, p.id)
    run = svc.execute(ALICE, p.id)
    assert run.status == "COMPLETED" and run.release == "RELEASED" and not run.reconciliation["failed"]
    stats = svc.adapters[rid].stats.public()
    assert stats["calls"] > 20 and stats["guard_trips"] == 0
    ref = RefreshService(tmp_path_factory.mktemp("ref"))
    b = ref.bootstrap_demo(ADMIN)
    ref.src_id, ref.tgt_id, ref.s4_src, ref.s4_tgt = b["source"]["id"], b["target"]["id"], b["s4_source"]["id"], b["s4_target"]["id"]
    rp = ready_project(ref)
    ref.submit(ALICE, rp.id)
    ref.approve(CAROL, rp.id)
    ref.execute(ALICE, rp.id)
    from .conftest import norm as tnorm
    assert tnorm(svc.adapters[svc.tgt_id].data) == tnorm(ref.adapters[ref.tgt_id].data)  # identical target state


def test_scope_filters_are_pushed_to_the_remote_system_not_scanned(tmp_path):
    svc, rid, _ = remote_svc(tmp_path)
    tr = svc.adapters[rid]._t._inner
    make_project(svc, src=rid, tgt=svc.tgt_id, company="1000", days=90)
    p = next(iter(svc.projects.values()))
    svc.build_plan(ALICE, p.id)
    assert set(svc.adapters[rid].stats.public()["scanned_tables"]) <= {"T001W"}  # only a 9-row customizing table was scanned; no business table was
    reads = [c for c in tr.calls if c["function"] == "RFC_READ_TABLE" and c["QUERY_TABLE"] == "VBAK"]
    assert reads and any(c.get("OPTIONS") for c in reads)  # the company-code and date filters travelled as WHERE clauses


def test_delta_from_a_remote_source_falls_back_to_a_full_compare(tmp_path):
    from rfactory.selective.manifest import Scope
    from .test_delta import SCHED, spec
    svc, rid, sim = remote_svc(tmp_path)
    s = spec(svc, source_id=rid, scopes=[{"scope": Scope(object_type="CUSTOMER", company_codes=["1000"])}], include_downstream=[], schedule=None)
    sc = svc.delta.create(ALICE, s)
    from rfactory.masking.engine import Rule
    sc.masking_policy.rules.append(Rule("KNA1", "ZZ_CONTACT_EMAIL", "EMAIL", "email"))
    svc.delta.submit(ALICE, sc.id)
    svc.delta.approve(CAROL, sc.id, now=datetime(2026, 10, 7, 12, tzinfo=timezone.utc))
    first = svc.delta.run(SCHED, sc.id)
    assert first["status"] == "COMPLETED" and first["new"] > 0
    again = svc.delta.run(SCHED, sc.id)
    assert again["new"] == 0 and again["changed"] == 0  # nothing re-copied, although no change documents exist
    k = next(r["KUNNR"] for r in sim.data["KNA1"] if sc.tracked and f"CUSTOMER:{r['KUNNR']}" in sc.tracked)
    sim.sim_update("KNA1", (k,), LAND1="CH")
    third = svc.delta.run(SCHED, sc.id)
    assert third["changed"] == 1
    assert svc.adapters[rid].capabilities()["change_documents"] is False
    with pytest.raises(ChangeLogGap):
        svc.adapters[rid].changes_since(0)


def test_remote_connections_survive_a_restart_as_profiles_and_are_reconnected_or_marked_disconnected(tmp_path, monkeypatch):
    svc, rid, sim = remote_svc(tmp_path, persist=True)
    svc.checkpoint()
    n = RefreshService(tmp_path, persist=True)  # no pyrfc here, so it cannot reconnect
    assert rid in n.systems and n.remote_profiles[rid].name == "remote" and not n.is_local(rid)
    assert isinstance(n.adapters[rid], R.DisconnectedAdapter)
    with pytest.raises(R.RemoteError, match="disconnected"):
        n.adapters[rid].select("T001")
    assert not n.readiness(rid)["ready"]
    assert "password" not in json.dumps(n.remote_profiles[rid].public()).lower().replace("password_ref", "")
    monkeypatch.setattr(RefreshService, "rebuild_remote", lambda self, system, profile: R.RfcSourceAdapter(system, FakeRfcTransport(sim), profile, sleep=NO_SLEEP))
    m = RefreshService(tmp_path, persist=True)
    assert len(m.adapters[rid].select("T001")) == 2 and m.readiness(rid)["ready"]


def test_scoped_users_cannot_connect_outside_their_scope(tmp_path):
    svc = RefreshService(tmp_path)
    cora = U["cora.regional"]
    prof = ConnectionProfile("x", "rfc", ashost="h", client="100")
    sys_ = SapSystem(sid="S4X", client="100", role="PRD")
    scoped = cora.__class__("c", "c", ("admin",), attrs={"systems": ["EP1"]})
    assert scoped.can("system:write")
    with pytest.raises(Forbidden, match="outside your scope"):
        svc.connect_remote(scoped, sys_, prof, transport=FakeRfcTransport(make_demo_pair()[0]))


def test_remote_sources_over_http(tmp_path):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    c = TestClient(create_app(tmp_path, persist=False))
    H = lambda u: {"X-Demo-User": u}
    c.post("/api/demo/bootstrap", headers=H("root.admin"))
    assert c.post("/api/demo/connect-fake-rfc", headers=H("tina.tester")).status_code == 403
    r = c.post("/api/demo/connect-fake-rfc", headers=H("alice.basis"))
    assert r.status_code == 201 and r.json()["remote"] and r.json()["simulated_transport"]
    rid = r.json()["id"]
    info = c.get(f"/api/systems/{rid}/remote", headers=H("erin.auditor")).json()
    assert info["validated_against_real_sap"] is False and info["capabilities"]["writes"] is False and "password" not in json.dumps(info["profile"]).lower().replace("password_ref", "")
    assert c.get(f"/api/systems/{rid}/readiness", headers=H("erin.auditor")).json()["ready"]
    local = c.get("/api/systems", headers=H("erin.auditor")).json()[0]["id"]
    assert c.get(f"/api/systems/{local}/remote", headers=H("erin.auditor")).status_code == 409
    body = {"system": {"sid": "EP9", "client": "100", "role": "PRD"}, "profile": {"name": "real", "kind": "rfc", "ashost": "sap.example", "client": "100"}}
    r2 = c.post("/api/systems/connect", json=body, headers=H("alice.basis"))
    assert r2.status_code == 409 and "pyrfc" in r2.json()["detail"]
    assert c.post("/api/systems/connect", json={"system": {"sid": "E"}, "profile": {"bogus": 1}}, headers=H("alice.basis")).status_code == 422
    assert c.post("/api/systems/connect", json=body, headers=H("refresh.copilot")).status_code == 403
