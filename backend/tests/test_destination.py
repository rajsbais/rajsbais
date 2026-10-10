"""Connection parameters of a system through the API: masked document, secrets refused, env references kept,
unknown keys refused, transport switch, other metadata untouched, audit."""

from sqlalchemy import select

from sdtf.models import AuditEvent, SapSystem

API = "/api/v1"


def test_destination_document_and_update(client, tokens, slice_result, session):
    pid = slice_result["project_id"]
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "ECD", "client": "100", "role": "SOURCE", "product": "ECC", "release": "6.0 EHP8", "connector": "RFC", "meta": {"rfc": {"transport": "simulated", "assets": {"source": "net"}}}}, headers=tokens["architect"])
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    d = client.get(f"{API}/systems/{sid}/destination", headers=tokens["viewer"]).json()
    assert d["connector"] == "RFC" and d["rfc"]["transport"] == "simulated" and d["rfc"]["dest"] == {} and d["rfc"]["password_env"] == "SDTF_RFC_DEST_ECD_PASSWD" and "api" not in d
    assert client.put(f"{API}/systems/{sid}/destination", json={"dest": {"ashost": "x"}}, headers=tokens["viewer"]).status_code == 403
    # secrets are refused, env references are kept, unknown keys refused, bad transport refused
    for bad, code, msg in (({"dest": {"ashost": "h", "passwd": "secret"}}, 400, "never stored"), ({"dest": {"ashost": "h", "snc_myname": "x"}}, 400, "never stored"), ({"dest": {"ashost": "h", "ssh": "x"}}, 400, "unknown connection key"), ({"transport": "https", "dest": {}}, 400, "transport must be")):
        rr = client.put(f"{API}/systems/{sid}/destination", json=bad, headers=tokens["architect"])
        assert rr.status_code == code and msg in rr.json()["detail"], (bad, rr.text)
    rr = client.put(f"{API}/systems/{sid}/destination", json={"transport": "pyrfc", "dest": {"ashost": "vhcalnplci", "sysnr": "00", "client": "001", "user": "DEVELOPER", "passwd": "env:NPL_PW", "lang": ""}}, headers=tokens["architect"])
    assert rr.status_code == 200, rr.text
    d = rr.json()
    assert d["rfc"]["transport"] == "pyrfc" and d["rfc"]["dest"] == {"ashost": "vhcalnplci", "sysnr": "00", "client": "001", "user": "DEVELOPER", "passwd": "***"} and d["connector_status"] != "SIMULATED"
    session.expire_all()
    s = session.get(SapSystem, sid)
    assert s.meta["rfc"]["dest"]["passwd"] == "env:NPL_PW" and s.meta["rfc"]["assets"] == {"source": "net"} and s.meta["rfc"]["transport"] == "pyrfc"
    ev = session.execute(select(AuditEvent).where(AuditEvent.action == "DESTINATION_CHANGED", AuditEvent.subject_id == sid)).scalars().all()
    assert ev and ev[-1].details["keys"] == ["ashost", "client", "passwd", "sysnr", "user"] and ev[-1].details["kind"] == "rfc"
    # back to the simulator: the status follows
    rr = client.put(f"{API}/systems/{sid}/destination", json={"transport": "simulated", "dest": {}}, headers=tokens["architect"])
    assert rr.status_code == 200 and rr.json()["connector_status"] == "SIMULATED"
    # API targets carry both destinations; the API one takes base_url and refuses the RFC keys
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "S4D", "client": "100", "role": "TARGET", "product": "S4HANA", "release": "2023", "connector": "API", "meta": {"api": {"transport": "simulated"}}}, headers=tokens["architect"])
    tid = r.json()["id"]
    rr = client.put(f"{API}/systems/{tid}/destination?kind=api", json={"transport": "https", "dest": {"base_url": "https://vhcala4hci.dummy.nodomain:44300", "client": "100", "user": "DEVELOPER", "verify_tls": False}}, headers=tokens["architect"])
    assert rr.status_code == 200, rr.text
    assert rr.json()["api"]["dest"] == {"base_url": "https://vhcala4hci.dummy.nodomain:44300", "client": "100", "user": "DEVELOPER", "verify_tls": False} and rr.json()["api"]["password_env"] == "SDTF_S4_API_S4D_PASSWD"
    assert client.put(f"{API}/systems/{tid}/destination?kind=api", json={"dest": {"ashost": "h"}}, headers=tokens["architect"]).status_code == 400
    rr = client.put(f"{API}/systems/{tid}/destination?kind=rfc", json={"transport": "simulated", "dest": {"ashost": "vhcala4hci", "sysnr": "00", "client": "100", "user": "DEVELOPER"}}, headers=tokens["architect"])
    assert rr.status_code == 200 and rr.json()["rfc"]["transport"] == "simulated" and rr.json()["api"]["transport"] == "https"
    # a synthetic source has neither
    src = session.get(SapSystem, slice_result["source_id"])
    if src.connector == "SYNTHETIC":
        assert client.put(f"{API}/systems/{src.id}/destination", json={"dest": {}}, headers=tokens["architect"]).status_code == 409
    assert client.put(f"{API}/systems/{sid}/destination?kind=api", json={"dest": {}}, headers=tokens["architect"]).status_code == 409
