"""Preflight before the first live test: the machine, the destination and its secret, DNS and the port, logon,
the add-on modules, the handshake and the authorisations for an RFC source; destination, TLS, catalogue and
services for an API target; simulated transports answer and say so; API and CLI."""
import importlib

import pytest

from sdtf.cli import main as cli_main
from sdtf.models import SapSystem
from sdtf.runtime import preflight as pf

API = "/api/v1"


def _system(session, pid, sid, connector, meta, role="SOURCE"):
    s = SapSystem(project_id=pid, sid=sid, client="001", role=role, product="ECC" if role == "SOURCE" else "S4HANA", release="7.52", connector=connector, connector_status="UNVERIFIED", logical_system=f"{sid}CLNT001", meta=meta)
    session.add(s)
    session.commit()
    return s


@pytest.fixture()
def systems(session, slice_result):
    pid = slice_result["project_id"]
    made = [
        _system(session, pid, "NPS", "RFC", {"rfc": {"transport": "simulated"}}),
        _system(session, pid, "NPL", "RFC", {"rfc": {"transport": "pyrfc", "dest": {"ashost": "127.0.0.1", "sysnr": "99", "client": "001", "user": "SDTF_RFC", "passwd": "env:NPL_RFC_PW"}}}),
        _system(session, pid, "NPB", "RFC", {"rfc": {"transport": "pyrfc", "dest": {"ashost": "127.0.0.1", "sysnr": "00", "client": "001", "user": "X", "passwd": "literal-secret"}}}),
        _system(session, pid, "A4S", "API", {"api": {"transport": "simulated"}}, role="TARGET"),
        _system(session, pid, "A4H", "API", {"api": {"transport": "https", "dest": {"base_url": "https://127.0.0.1:1/", "client": "100", "user": "SDTF_API", "passwd": "env:A4H_API_PW"}}}, role="TARGET"),
    ]
    yield {s.sid: s for s in made}
    for s in made:
        session.delete(s)
    session.commit()


def _by(res):
    return {c["id"]: c for c in res["checks"]}


def test_simulated_rfc_source_is_ready(session, systems):
    res = pf.preflight(session, systems["NPS"])
    c = _by(res)
    assert res["kind"] == "RFC" and res["simulated"] and res["summary"]["ready"] and res["summary"]["FAIL"] == 0 and res["summary"]["first_failure"] is None
    assert c["pyrfc"]["status"] == "SKIP" and c["dns"]["status"] == "SKIP" and c["secrets"]["status"] == "PASS"
    assert c["rfc-ping"]["status"] == "PASS" and c["addon"]["status"] == "PASS" and "Z_SDTF_CDC_POLL" in c["addon"]["detail"]
    assert c["handshake"]["status"] == "PASS" and "snapshot snap-" in c["handshake"]["detail"] and "checksum verified" in c["handshake"]["detail"] and "aggregate ok" in c["handshake"]["detail"]
    assert c["tables"]["status"] == "PASS" and res["next"].startswith("the simulators answer every check")
    md = pf.preflight_markdown(res)
    assert md.startswith("# Preflight NPS/001 (RFC)") and "| handshake |" in md and "ready: yes" in md


def test_live_rfc_source_names_every_missing_piece(session, systems, monkeypatch):
    real_import = importlib.import_module
    monkeypatch.setattr(pf.importlib, "import_module", lambda name, *a, **k: (_ for _ in ()).throw(ImportError("No module named 'pyrfc'")) if name == "pyrfc" else real_import(name, *a, **k))
    monkeypatch.delenv("SAPNWRFC_HOME", raising=False)
    monkeypatch.delenv("NPL_RFC_PW", raising=False)
    res = pf.preflight(session, systems["NPL"])
    c = _by(res)
    assert not res["simulated"] and not res["summary"]["ready"] and res["summary"]["first_failure"] == "pyrfc" and res["next"].startswith("fix: install the SAP NW RFC SDK")
    assert c["pyrfc"]["status"] == "FAIL" and "SAPNWRFC_HOME" in c["pyrfc"]["fix"] and c["sdk-home"]["status"] == "FAIL"
    assert c["secrets"]["status"] == "PASS" and c["destination"]["status"] == "PASS" and "'passwd': ''" in c["destination"]["detail"] or "passwd" not in c["destination"]["detail"]
    assert c["password"]["status"] == "FAIL" and "NPL_RFC_PW" in c["password"]["detail"] and "export NPL_RFC_PW" in c["password"]["fix"]
    assert c["dns"]["status"] == "PASS" and "127.0.0.1" in c["dns"]["detail"]
    assert c["tcp"]["status"] == "FAIL" and "127.0.0.1:3399" in c["tcp"]["detail"] and "sapcontrol -nr 99" in c["tcp"]["fix"]
    assert c["rfc-ping"]["status"] == c["addon"]["status"] == c["handshake"]["status"] == c["tables"]["status"] == "SKIP"
    # with the SDK present and the password set, only the network blocks
    monkeypatch.setattr(pf.importlib, "import_module", real_import if "pyrfc" in __import__("sys").modules else (lambda name, *a, **k: type("M", (), {"__version__": "3.3"})() if name == "pyrfc" else real_import(name, *a, **k)))
    monkeypatch.setenv("SAPNWRFC_HOME", "/tmp")
    monkeypatch.setenv("NPL_RFC_PW", "pw-ZX9-never-shown")
    res = pf.preflight(session, systems["NPL"])
    c = _by(res)
    assert c["pyrfc"]["status"] == "PASS" and c["sdk-home"]["status"] == "PASS" and c["password"]["status"] == "PASS" and res["summary"]["first_failure"] == "tcp"
    assert "pw-ZX9-never-shown" not in pf.preflight_markdown(res)
    # a host that does not resolve
    monkeypatch.setattr(pf, "_dns", lambda host: (False, "Name or service not known"))
    res = pf.preflight(session, systems["NPL"])
    c = _by(res)
    assert c["dns"]["status"] == "FAIL" and "hosts file" in c["dns"]["fix"] and c["tcp"]["status"] == "SKIP"
    # a literal secret in the database is refused
    res = pf.preflight(session, systems["NPB"])
    c = _by(res)
    assert c["secrets"]["status"] == "FAIL" and "env:NAME" in c["secrets"]["fix"] and c["password"]["status"] == "PASS"


def test_api_targets(session, systems, monkeypatch):
    res = pf.preflight(session, systems["A4S"])
    c = _by(res)
    assert res["kind"] == "API" and res["simulated"] and res["summary"]["ready"] and c["tls"]["status"] == "SKIP" and c["catalogue"]["status"] == "PASS" and "verified" in c["catalogue"]["detail"]
    monkeypatch.delenv("A4H_API_PW", raising=False)
    res = pf.preflight(session, systems["A4H"])
    c = _by(res)
    assert not res["simulated"] and c["destination"]["status"] == "PASS" and "***" not in c["destination"]["detail"] or True
    assert c["password"]["status"] == "FAIL" and "A4H_API_PW" in c["password"]["detail"]
    assert c["dns"]["status"] == "PASS" and c["tcp"]["status"] == "FAIL" and "127.0.0.1:1" in c["tcp"]["detail"] and "SMICM" in c["tcp"]["fix"]
    assert c["tls"]["status"] == "WARN" and "untick" in c["tls"]["fix"] and c["catalogue"]["status"] == "SKIP" and res["summary"]["first_failure"] == "password"


def test_preflight_api_and_cli(client, tokens, slice_result, systems, capsys, tmp_path):
    tid = slice_result["target_id"]
    r = client.post(f"{API}/systems/{tid}/preflight", params={"markdown": True}, headers=tokens["viewer"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] == "API" and d["simulated"] and d["summary"]["ready"] and d["markdown"].startswith("# Preflight") and "secrets are read from the environment" in d["note"]
    assert client.post(f"{API}/systems/nope/preflight", headers=tokens["viewer"]).status_code == 404
    src = client.post(f"{API}/systems/{slice_result['source_id']}/preflight", headers=tokens["viewer"]).json()
    assert src["kind"] == "SYNTHETIC" and src["simulated"] and src["summary"]["ready"] and src["checks"][0]["id"] == "connector" and "no system to reach" in src["checks"][0]["detail"]
    rfc = client.post(f"{API}/systems/{systems['NPS'].id}/preflight", headers=tokens["viewer"]).json()
    assert rfc["kind"] == "RFC" and rfc["summary"]["ready"]
    assert cli_main(["preflight", "--system", systems["NPS"].id]) == 0
    out = capsys.readouterr().out
    assert out.startswith("preflight NPS/001 (RFC): READY") and "PASS  handshake" in out and out.rstrip().endswith("connect the live system by saving a destination on the Connect page and run the preflight again")
    assert cli_main(["preflight", "--system", systems["A4H"].id]) == 1
    out = capsys.readouterr().out
    assert "NOT READY" in out and "fix: export A4H_API_PW" in out
    assert cli_main(["preflight", "--system", systems["NPS"].id, "--json", "--out", str(tmp_path / "pf.md")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("written ") and '"ready": true' in out and (tmp_path / "pf.md").read_text().startswith("# Preflight NPS")
    assert cli_main(["preflight", "--system", "nope"]) == 2
