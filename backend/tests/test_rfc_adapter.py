"""RFC extraction adapter against the ABAP add-on contract (sap-abap/README.md).

The simulated add-on is the executable contract; the pyrfc binding is exercised with a fake `pyrfc` module.
Nothing here talks to an SAP system."""
import json
import sys
import types

import pytest

from sdtf.catalog.store import RecordStore
from sdtf.catalog.tables import record_key
from sdtf.demo import create_demo_project, demo_scope_definition, run_vertical_slice
from sdtf.runtime import rfc
from sdtf.runtime.extraction import RfcExtractor, SyntheticStoreExtractor, build_extractor, run_extraction
from sdtf.scope.service import create_manifest
from sdtf.staging import get_backend
from sdtf.synthetic.ecc_generator import LandscapeSpec, generate_landscape

API = "/api/v1"


@pytest.fixture(scope="module")
def store():
    return RecordStore.from_tables("sim", generate_landscape(LandscapeSpec(seed=7, scale=1)))


@pytest.fixture
def addon(store):
    return rfc.SimulatedAbapAddon(store, snapshot_ttl=60)


def _snap(addon):
    return addon.call(rfc.FM_OPEN_SNAPSHOT)["EV_SNAPSHOT"]


# ----------------------------------------------------------------------------------------- contract behaviour
def test_snapshot_token_required_and_expires(store):
    now = [1000.0]
    a = rfc.SimulatedAbapAddon(store, snapshot_ttl=30, clock=lambda: now[0])
    with pytest.raises(rfc.RfcError, match="SNAPSHOT_UNKNOWN"):
        a.call(rfc.FM_READ_PACKAGE, IV_TABLE="T001", IV_SNAPSHOT="snap-nope")
    r = a.call(rfc.FM_OPEN_SNAPSHOT, IT_TABLES=[{"TABNAME": "T001"}])
    assert r["EV_SNAPSHOT"].startswith("snap-") and r["ET_WATERMARKS"] == [{"TABNAME": "T001", "WATERMARK": f"rows={store.count('T001')}"}]
    assert a.call(rfc.FM_READ_PACKAGE, IV_TABLE="T001", IV_SNAPSHOT=r["EV_SNAPSHOT"])["EV_EOF"] == "X"
    now[0] += 31
    with pytest.raises(rfc.RfcError, match="SNAPSHOT_EXPIRED"):
        a.call(rfc.FM_READ_PACKAGE, IV_TABLE="T001", IV_SNAPSHOT=r["EV_SNAPSHOT"])


def test_authorization_and_unknown_tables(store):
    a = rfc.SimulatedAbapAddon(store, allowed_tables={"T001", "KNA1"})
    s = _snap(a)
    assert a.call(rfc.FM_TABLE_METADATA, IV_TABLE="T001")["EV_AUTHORIZED"] == "X"
    with pytest.raises(rfc.RfcError, match="NOT_AUTHORIZED"):
        a.call(rfc.FM_READ_PACKAGE, IV_TABLE="BKPF", IV_SNAPSHOT=s)
    with pytest.raises(rfc.RfcError, match="TABLE_UNKNOWN"):
        a.call(rfc.FM_READ_PACKAGE, IV_TABLE="NOPE", IV_SNAPSHOT=s)
    with pytest.raises(rfc.RfcError, match="FU_NOT_FOUND"):
        a.call("Z_SDTF_WRITE_ROWS")
    assert a.call(rfc.FM_CDC_POLL, IV_WATERMARK="0")["EV_EOF"] == "X"  # no change log yet: nothing to deliver


def test_predicate_semantics_follow_sap_ranges():
    row = {"BUKRS": "5000", "GJAHR": "2024", "BELNR": "0100000123"}
    m = rfc.predicates_match
    assert m(row, [])
    assert m(row, [rfc.predicate("BUKRS", "EQ", "5000")]) and not m(row, [rfc.predicate("BUKRS", "EQ", "1000")])
    # same field: positives OR-ed; different fields AND-ed
    assert m(row, [rfc.predicate("BUKRS", "EQ", "1000"), rfc.predicate("BUKRS", "EQ", "5000")])
    assert not m(row, [rfc.predicate("BUKRS", "EQ", "5000"), rfc.predicate("GJAHR", "EQ", "2023")])
    assert m(row, [rfc.predicate("GJAHR", "BT", "2023", "2025")]) and not m(row, [rfc.predicate("GJAHR", "NB", "2023", "2025")])
    assert m(row, [rfc.predicate("GJAHR", "GE", "2024"), rfc.predicate("GJAHR", "LT", "2025")])
    assert m(row, [rfc.predicate("BELNR", "CP", "01000001*")]) and not m(row, [rfc.predicate("BELNR", "NP", "0100000+23")])
    # an exclusion on a field combines with the positive ranges of the same field
    assert not m(row, [rfc.predicate("BUKRS", "EQ", "5000"), rfc.predicate("BUKRS", "NE", "5000")])
    with pytest.raises(ValueError):
        rfc.predicate("BUKRS", "LIKE", "x")


def test_packages_are_ordered_checksummed_and_resumable(store, addon):
    s = _snap(addon)
    preds = [rfc.predicate("BUKRS", "EQ", "5000")]
    all_rows = sorted((r for r in store.rows("BKPF") if r["BUKRS"] == "5000"), key=lambda r: (r["BUKRS"], r["BELNR"], r["GJAHR"]))
    assert len(all_rows) > 7
    got, cursor, eof, packages = [], "", False, 0
    while not eof:
        r = addon.call(rfc.FM_READ_PACKAGE, IV_TABLE="BKPF", IT_PREDICATE=preds, IV_PACKAGE=7, IV_CURSOR=cursor, IV_SNAPSHOT=s)
        json_rows = [e["JSON"] for e in r["ET_ROWS"]]
        assert r["EV_CHECKSUM"] == rfc.package_checksum(json_rows) and len(json_rows) <= 7
        assert [e["ROWNO"] for e in r["ET_ROWS"]] == list(range(1, len(json_rows) + 1))
        got += [json.loads(j) for j in json_rows]
        cursor, eof, packages = r["EV_CURSOR"], r["EV_EOF"] == "X", packages + 1
    assert got == all_rows and packages == -(-len(all_rows) // 7)
    assert addon.stats.max_package == 7 and addon.stats.full_scans == 0
    # the package size is clamped server-side
    big = rfc.SimulatedAbapAddon(store, server_max_package=5)
    r = big.call(rfc.FM_READ_PACKAGE, IV_TABLE="BKPF", IT_PREDICATE=preds, IV_PACKAGE=50000, IV_SNAPSHOT=_snap(big))
    assert len(r["ET_ROWS"]) == 5 and r["EV_EOF"] == ""


def test_cursor_is_bound_to_table_predicate_and_snapshot(store, addon):
    s = _snap(addon)
    r = addon.call(rfc.FM_READ_PACKAGE, IV_TABLE="BKPF", IT_PREDICATE=[], IV_PACKAGE=3, IV_SNAPSHOT=s)
    cur = r["EV_CURSOR"]
    with pytest.raises(rfc.RfcError, match="INVALID_CURSOR"):
        addon.call(rfc.FM_READ_PACKAGE, IV_TABLE="BSEG", IV_PACKAGE=3, IV_CURSOR=cur, IV_SNAPSHOT=s)
    with pytest.raises(rfc.RfcError, match="INVALID_CURSOR"):
        addon.call(rfc.FM_READ_PACKAGE, IV_TABLE="BKPF", IT_PREDICATE=[rfc.predicate("BUKRS", "EQ", "5000")], IV_PACKAGE=3, IV_CURSOR=cur, IV_SNAPSHOT=s)
    with pytest.raises(rfc.RfcError, match="INVALID_CURSOR"):
        addon.call(rfc.FM_READ_PACKAGE, IV_TABLE="BKPF", IV_PACKAGE=3, IV_CURSOR=cur, IV_SNAPSHOT=_snap(addon))
    with pytest.raises(rfc.RfcError, match="INVALID_CURSOR"):
        addon.call(rfc.FM_READ_PACKAGE, IV_TABLE="BKPF", IV_PACKAGE=3, IV_CURSOR="garbage", IV_SNAPSHOT=s)
    with pytest.raises(rfc.RfcError, match="INVALID_PREDICATE"):
        addon.call(rfc.FM_READ_PACKAGE, IV_TABLE="BKPF", IT_PREDICATE=[{"FIELD": "BUKRS", "OP": "LIKE", "LOW": "5"}], IV_SNAPSHOT=s)


class TamperingTransport:
    """Wraps a transport and corrupts one row of every package after the checksum was computed."""

    name = "TAMPER"

    def __init__(self, inner):
        self.inner = inner

    def call(self, fn, **params):
        r = self.inner.call(fn, **params)
        if fn == rfc.FM_READ_PACKAGE and r["ET_ROWS"]:
            row = json.loads(r["ET_ROWS"][0]["JSON"])
            row["WRBTR"] = "999999.99"
            r["ET_ROWS"][0]["JSON"] = rfc.row_json(row)
        return r


def test_client_verifies_checksums_and_drives_cursors(store, addon):
    c = rfc.AbapAddonClient(addon, package_size=11)
    rows = list(c.read_all("KNA1", []))
    assert len(rows) == store.count("KNA1") and c.packages == -(-len(rows) // 11) and c.snapshot
    assert set(c.read_keyed("T001K", [])) == {record_key("T001K", r) for r in store.rows("T001K")}
    md = c.table_metadata("BSEG")
    assert md["key_fields"][:2] == ["BUKRS", "BELNR"] and md["rows"] == store.count("BSEG") and md["authorized"]
    bad = rfc.AbapAddonClient(TamperingTransport(rfc.SimulatedAbapAddon(store)), package_size=50)
    with pytest.raises(rfc.RfcIntegrityError):
        list(bad.read_all("BSEG", [rfc.predicate("BUKRS", "EQ", "5000")]))


# ------------------------------------------------------------------------------------------- the extractor
@pytest.fixture
def rfc_project(session):
    db = session
    ctx = create_demo_project(db, scale=1, seed=11, connector="RFC")
    from sdtf.discovery.service import discover_system
    from sdtf.graph.service import build_graph, persist_graph

    store = RecordStore.load(db, ctx["source"].id)
    discover_system(db, ctx["source"], "architect", store)
    persist_graph(db, ctx["source"].id, build_graph(store, ctx["source"].id))
    m = create_manifest(db, ctx["project"].id, demo_scope_definition(ctx["source"], ctx["target"]), "architect")
    return ctx, store, m


def test_rfc_extractor_matches_synthetic_extractor_with_pushdown(rfc_project):
    ctx, store, m = rfc_project
    cls, ccs = m.selection["classification"], set(m.definition["company_codes"])
    addon = rfc.SimulatedAbapAddon(store)
    ex_rfc = RfcExtractor(addon, cls, ccs, package_size=500, key_chunk=50, key_pushdown_limit=2000)
    ex_syn = SyntheticStoreExtractor(store, cls, ccs)
    assert [p.id for p in ex_rfc.partitions()] == [p.id for p in ex_syn.partitions()]
    assert ex_rfc.snapshot().startswith("snap-") and ex_rfc.snapshot() == ex_rfc.snapshot()  # one token per run
    got = {(r.table, r.key) for p in ex_rfc.partitions() for r in ex_rfc.extract(p)}
    want = {(r.table, r.key) for p in ex_syn.partitions() for r in ex_syn.extract(p)}
    assert got == want and len(got) > 500
    assert ex_rfc.retained_rows == ex_syn.retained_rows and ex_rfc.reference_stubs == ex_syn.reference_stubs
    st = ex_rfc.adapter_stats()
    assert st["transport"] == "SIMULATED_ADDON" and st["packages"] >= st["rfc_calls"] - 2 and st["rows_transferred"] >= len(got)
    # every application-table read carried a predicate (T001K is the only full read); packages respect the size
    assert addon.stats.full_scans == 1 and addon.stats.max_package <= 500
    assert st["pushdown"]["key_ranges"] > 0 and st["pushdown"]["org_predicate"] > 0 and st["pushdown"]["org_only"] == 0
    # large partitions fall back to the organisational predicate and client-side key filtering - same rows
    ex_big = RfcExtractor(rfc.SimulatedAbapAddon(store), cls, ccs, package_size=500, key_pushdown_limit=1)
    assert {(r.table, r.key) for p in ex_big.partitions() for r in ex_big.extract(p)} == want
    assert ex_big.pushdown["org_only"] > 0 and ex_big.rows_discarded > 0


def test_build_extractor_by_connector_and_transport(session, rfc_project):
    db = session
    ctx, store, m = rfc_project
    src = ctx["source"]
    assert src.connector == "RFC" and src.connector_status == "SIMULATED"
    ex = build_extractor(db, src, m.selection["classification"], set(m.definition["company_codes"]))
    assert isinstance(ex, RfcExtractor) and ex.client.t.name == "SIMULATED_ADDON"
    src.meta = {**src.meta, "rfc": {"transport": "auto"}}
    with pytest.raises(rfc.RfcUnavailable, match="SDTF_RFC_DEST_ECP"):
        build_extractor(db, src, {}, set())
    src.meta = {**src.meta, "rfc": {"transport": "simulated"}}
    backend = get_backend("columnar", session=db)  # no run row needed for the file-based backend
    metrics = run_extraction(db, "run-rfc-test", ex, {}, workers=3, backend=backend)
    assert metrics["adapter"] == "RFC" and metrics["adapter_stats"]["packages"] > 0 and metrics["records"] > 500
    assert metrics["snapshot_id"] == ex.snapshot()


def test_vertical_slice_over_rfc_reconciles(session):
    db = session
    out = run_vertical_slice(db, scale=1, seed=5, connector="RFC")
    run = out["run"]
    assert run.status == "COMPLETED" and run.report["reconciliation"]["overall"] == "PASS"
    ex = next(s for s in run.stages if s.name == "EXTRACT")
    assert ex.metrics["adapter"] == "RFC" and ex.metrics["adapter_stats"]["transport"] == "SIMULATED_ADDON" and ex.metrics["adapter_stats"]["pushdown"]["key_ranges"] > 0
    assert run.snapshot_id.startswith("snap-") and run.snapshot_id == ex.metrics["adapter_stats"].get("snapshot", run.snapshot_id)


# ----------------------------------------------------------------------------------------- pyrfc binding
class FakeConnection:
    def __init__(self, **dest):
        self.dest = dest
        self.calls = []

    def call(self, fn, **params):
        self.calls.append((fn, params))
        if fn == rfc.FM_OPEN_SNAPSHOT:
            return {"EV_SNAPSHOT": "snap-live", "EV_VALID_UNTIL": "20991231000000", "ET_WATERMARKS": []}
        if fn == rfc.FM_READ_PACKAGE:
            if params["IV_TABLE"] == "BSEG":
                raise FakeAbapError("NOT_AUTHORIZED", "No authorization for BSEG")
            rows = [rfc.row_json({"BUKRS": "5000", "BUTXT": "SpinCo"})]
            return {"ET_ROWS": [{"ROWNO": 1, "JSON": rows[0]}], "EV_CURSOR": "", "EV_EOF": "X", "EV_CHECKSUM": rfc.package_checksum(rows)}
        raise AssertionError(fn)

    def close(self):
        pass


class FakeAbapError(Exception):
    def __init__(self, key, message):
        super().__init__(message)
        self.key, self.message = key, message


def test_pyrfc_transport_maps_parameters_and_errors(monkeypatch):
    fake = types.ModuleType("pyrfc")
    fake.Connection = FakeConnection
    fake.ABAPApplicationError = FakeAbapError
    monkeypatch.setitem(sys.modules, "pyrfc", fake)
    monkeypatch.setenv("SDTF_RFC_DEST_ECP", json.dumps({"ashost": "ecp.example.com", "sysnr": "00", "client": "100", "user": "SDTF_READ", "passwd": "env:ECP_PW"}))
    monkeypatch.setenv("ECP_PW", "s3cret")
    dest = rfc.resolve_destination("ECP")
    assert dest["passwd"] == "s3cret" and rfc.mask_destination(dest)["passwd"] == "***" and rfc.mask_destination(dest)["user"] == "SDTF_READ"
    t = rfc.make_transport("ECP", {"rfc": {"transport": "auto"}})
    assert isinstance(t, rfc.PyRfcTransport) and t._conn.dest["ashost"] == "ecp.example.com"
    c = rfc.AbapAddonClient(t, package_size=100)
    rows, cur, eof = c.read_package("t001", [rfc.predicate("bukrs", "eq", "5000")])
    assert rows == [{"BUKRS": "5000", "BUTXT": "SpinCo"}] and eof and c.snapshot == "snap-live"
    fn, params = t._conn.calls[-1]
    assert fn == "Z_SDTF_READ_PACKAGE" and params == {"IV_TABLE": "T001", "IT_PREDICATE": [{"FIELD": "BUKRS", "OP": "EQ", "LOW": "5000", "HIGH": ""}], "IV_PACKAGE": 100, "IV_CURSOR": "", "IV_SNAPSHOT": "snap-live"}
    with pytest.raises(rfc.RfcError, match="NOT_AUTHORIZED: No authorization for BSEG"):
        c.read_package("BSEG", [])
    t.close()


def test_pyrfc_missing_gives_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyrfc", None)
    monkeypatch.delenv("SDTF_RFC_DEST_ECP", raising=False)
    with pytest.raises(rfc.RfcUnavailable, match="no RFC destination"):
        rfc.make_transport("ECP", {})
    with pytest.raises(rfc.RfcUnavailable, match="pyrfc"):
        rfc.PyRfcTransport({"ashost": "x"})


# -------------------------------------------------------------------------------------------------- API
def test_api_registers_rfc_system_tests_connector_and_hides_secrets(client, tokens):
    arch = tokens["architect"]
    pid = client.post(f"{API}/projects", json={"name": "rfc-api", "scenario_type": "CARVE_OUT"}, headers=arch).json()["id"]
    assert client.get(f"{API}/catalog/adapters", headers=arch).json()["RFC"]["status"] == "IMPLEMENTED"
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "ECQ", "role": "SOURCE", "product": "ECC", "release": "6.0 EHP8", "connector": "RFC", "meta": {"rfc": {"transport": "pyrfc", "dest": {"ashost": "h", "passwd": "plain"}}}}, headers=arch)
    assert r.status_code == 400 and "never stored" in r.json()["detail"]
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "ECQ", "role": "SOURCE", "product": "ECC", "release": "6.0 EHP8", "connector": "RFC", "meta": {"rfc": {"transport": "simulated", "allowed_tables": ["T001", "T001K"]}}}, headers=arch)
    assert r.status_code == 201 and r.json()["connector_status"] == "SIMULATED"
    sid = r.json()["id"]
    assert client.post(f"{API}/systems/{sid}/import-synthetic", json={"scale": 1, "seed": 3}, headers=arch).status_code == 200
    t = client.post(f"{API}/systems/{sid}/connector/test", headers=arch).json()
    assert t["ok"] and t["transport"] == "SIMULATED_ADDON" and t["table"]["key_fields"] == ["BUKRS"] and t["sample_rows"] == 5 and t["checksum_verified"]
    # a system pointing at a real destination without pyrfc reports the exact problem instead of failing silently
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "ECR", "role": "SOURCE", "product": "ECC", "release": "6.0 EHP8", "connector": "RFC"}, headers=arch)
    assert r.status_code == 201 and r.json()["connector_status"] == "IMPLEMENTED"
    t = client.post(f"{API}/systems/{r.json()['id']}/connector/test", headers=arch).json()
    assert t["ok"] is False and t["error"] == "RFC_UNAVAILABLE" and "SDTF_RFC_DEST_ECR" in t["detail"]
    assert client.post(f"{API}/systems/{r.json()['id']}/import-synthetic", json={}, headers=arch).status_code == 409
    assert client.post(f"{API}/systems/{sid}/connector/test", headers=tokens["viewer"]).status_code == 403
    # the demo endpoint can build the whole project on the RFC path
    d = client.post(f"{API}/projects/demo", json={"scale": 1, "seed": 2, "connector": "RFC", "name": "rfc demo"}, headers=arch)
    assert d.status_code == 201 and d.json()["project"]["systems"][0]["connector"] == "RFC"
