"""Read configuration of the reconciliation per system: API document, validation, storage under meta.rfc without
touching the transport, audit, effect on the chains."""

import pytest
from sqlalchemy import select

from sdtf.models import AuditEvent, SapSystem
from sdtf.reconciliation import read_config as rc
from sdtf.reconciliation import views

API = "/api/v1"


def _register(client, tokens, pid, sid="S4R", meta=None):
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": sid, "client": "100", "role": "TARGET", "product": "S4HANA", "release": "2023", "connector": "API", "meta": meta or {"api": {"transport": "simulated"}, "rfc": {"transport": "simulated", "dest": {"ashost": "vhcala4hci", "sysnr": "00", "client": "100", "user": "DEVELOPER"}}}}, headers=tokens["architect"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_read_config_document_validation_storage_and_audit(client, tokens, slice_result, session):
    pid = slice_result["project_id"]
    sid = _register(client, tokens, pid)
    d = client.get(f"{API}/systems/{sid}/read-config", headers=tokens["viewer"]).json()
    assert d["applies"] and d["s4hana"] and d["configured"] == {} and d["effective"]["journal_table"] == "auto" and d["effective"]["ledger"] == "0L"
    assert d["effective"]["assets"]["source"] == "auto" and d["effective"]["inventory"]["source"] == "auto" and d["effective"]["inventory"]["period_source"] == "calendar month of the read"
    assert {o["value"] for o in d["options"]["inventory_source"]} == {"auto", "mbew", "ml_period", "journal"} and d["notes"] and d["defaults"]["assets"]["area"] == "01"
    # viewers read, architects write
    assert client.put(f"{API}/systems/{sid}/read-config", json={"ledger": "2L"}, headers=tokens["viewer"]).status_code == 403
    body = {"journal_table": "acdoca", "ledger": "2l", "assets": {"source": "apc_items", "area": "1", "apc_movement_categories": "10, 20", "apc_tables": ["ACDOCA"]}, "inventory": {"source": "ml_period", "currency_type": "10", "period": {"year": "2026", "poper": "9"}, "inventory_accounts": ["300000"]}}
    r = client.put(f"{API}/systems/{sid}/read-config", json=body, headers=tokens["architect"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["configured"] == {"journal_table": "ACDOCA", "ledger": "2L", "assets": {"source": "apc_items", "area": "01", "apc_movement_categories": ["10", "20"], "apc_tables": ["ACDOCA"]}, "inventory": {"source": "ml_period", "currency_type": "10", "period": {"year": "2026", "poper": "009"}, "inventory_accounts": ["300000"]}}
    assert d["effective"]["journal_table"] == "ACDOCA" and d["effective"]["ledger"] == "2L" and d["effective"]["inventory"]["period"] == {"year": "2026", "poper": "009"} and d["effective"]["inventory"]["period_source"] == "configured"
    # stored under meta.rfc next to the untouched transport and destination; the chains read it
    session.expire_all()
    s = session.get(SapSystem, sid)
    assert s.meta["rfc"]["transport"] == "simulated" and s.meta["rfc"]["dest"]["user"] == "DEVELOPER" and s.meta["api"] == {"transport": "simulated"}
    assert views.asset_config(s)["apc_movement_categories"] == ["10", "20"] and views.inventory_config(s)["period"] == {"year": "2026", "poper": "009"} and views.inventory_config(s)["source"] == "ml_period"
    ev = session.execute(select(AuditEvent).where(AuditEvent.action == "READ_CONFIG_CHANGED", AuditEvent.subject_id == sid)).scalars().all()
    assert ev and ev[-1].details["configured"]["ledger"] == "2L"
    # the registration document shows it too
    proj = client.get(f"{API}/projects/{pid}", headers=tokens["viewer"]).json()
    assert next(x for x in proj["systems"] if x["id"] == sid)["meta"]["rfc"]["inventory"]["source"] == "ml_period"
    # invalid submissions name the field and change nothing
    for bad, msg in (({"journal_table": "GLT0"}, "journal_table"), ({"ledger": "LEDGER1"}, "ledger"), ({"assets": {"source": "apc_items"}}, "apc_movement_categories"), ({"assets": {"source": "guess"}}, "assets.source"), ({"assets": {"apc_tables": ["BSEG"]}}, "apc_tables"), ({"inventory": {"source": "journal"}}, "inventory_accounts"), ({"inventory": {"period": {"year": "26", "poper": "1"}}}, "period.year"), ({"inventory": {"period": {"year": "2026", "poper": "17"}}}, "period.poper"), ({"inventory": {"currency_type": "1X"}}, "currency_type"), ({"inventory": {"inventory_accounts": ["30 00"]}}, "inventory_accounts"), ({"rfc": {"transport": "pyrfc"}}, "unknown keys"), ({"transport": "pyrfc", "dest": {"passwd": "x"}}, "unknown keys"), ({"assets": {"dest": {}}}, "unknown fields")):
        r = client.put(f"{API}/systems/{sid}/read-config", json=bad, headers=tokens["architect"])
        assert r.status_code == 400 and msg in r.json()["detail"], (bad, r.text)
    session.expire_all()
    assert session.get(SapSystem, sid).meta["rfc"]["ledger"] == "2L"
    # an empty object resets to the defaults, the transport stays
    r = client.put(f"{API}/systems/{sid}/read-config", json={}, headers=tokens["architect"])
    assert r.status_code == 200 and r.json()["configured"] == {} and r.json()["effective"]["ledger"] == "0L"
    session.expire_all()
    assert session.get(SapSystem, sid).meta["rfc"] == {"transport": "simulated", "dest": {"ashost": "vhcala4hci", "sysnr": "00", "client": "100", "user": "DEVELOPER"}}
    # an API-only target: the document says it does not apply
    sid2 = _register(client, tokens, pid, sid="S4Q", meta={"api": {"transport": "simulated"}})
    d2 = client.get(f"{API}/systems/{sid2}/read-config", headers=tokens["viewer"]).json()
    assert d2["applies"] is False and d2["s4hana"] is True
    assert client.get(f"{API}/systems/nope/read-config", headers=tokens["viewer"]).status_code == 404


def test_validate_read_config_defaults_and_period_handling():
    cfg = rc.validate_read_config({})
    assert cfg == {"journal_table": "", "ledger": "0L", "assets": {"source": "auto", "area": "01", "apc_movement_categories": [], "apc_tables": ["ACDOCA", "FAAT_DOC_IT"]}, "inventory": {"source": "auto", "currency_type": "10", "period": None, "inventory_accounts": []}}
    assert rc.validate_read_config({"inventory": {"period": {"year": "", "poper": ""}}})["inventory"]["period"] is None
    assert rc.validate_read_config({"inventory": {"currency_type": "3"}})["inventory"]["currency_type"] == "03"
    with pytest.raises(rc.ReadConfigError, match="must be an object"):
        rc.validate_read_config({"inventory": {"period": "2026/009"}})
    with pytest.raises(rc.ReadConfigError, match="must be an object"):
        rc.validate_read_config([])
    s = SapSystem(sid="S4H", client="100", role="TARGET", product="S4HANA", release="2023", connector="API", meta={"api": {"transport": "simulated"}})
    assert rc.describe(s)["applies"] is False
    rc.apply(s, {"inventory": {"source": "mbew"}})
    assert s.meta["rfc"] == {"ledger": "0L", "assets": rc.validate_read_config({})["assets"], "inventory": {"source": "mbew", "currency_type": "10", "period": None, "inventory_accounts": []}} and s.meta["api"] == {"transport": "simulated"}
    rc.apply(s, {"journal_table": "BSEG"})
    assert s.meta["rfc"]["journal_table"] == "BSEG"
    rc.apply(s, {})
    assert s.meta["rfc"] == {}
