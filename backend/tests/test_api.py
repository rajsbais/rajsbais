import yaml

API = "/api/v1"


def test_health(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_auth_required_and_roles(client, tokens):
    assert client.get(f"{API}/projects").status_code == 401
    assert client.get(f"{API}/projects", headers={"Authorization": "Bearer abc.def"}).status_code == 401
    assert client.get(f"{API}/projects", headers=tokens["viewer"]).status_code == 200
    assert client.post(f"{API}/projects", json={"name": "x", "scenario_type": "SDT"}, headers=tokens["viewer"]).status_code == 403
    assert client.get(f"{API}/audit/events", headers=tokens["viewer"]).status_code == 403
    assert client.get(f"{API}/audit/events", headers=tokens["auditor"]).status_code == 200


def test_token_tamper_rejected(client, tokens):
    tok = tokens["admin"]["Authorization"].split(" ")[1]
    body, sig = tok.split(".")
    bad = {"Authorization": f"Bearer {body}.{sig[:-2]}xx"}
    assert client.get(f"{API}/auth/me", headers=bad).status_code == 401


def test_full_api_flow(client, tokens):
    arch, appr, op, viewer, aud = tokens["architect"], tokens["approver"], tokens["operator"], tokens["viewer"], tokens["auditor"]
    # 1. demo project with synthetic ECC landscape
    r = client.post(f"{API}/projects/demo", json={"scale": 1, "seed": 99, "name": "API flow"}, headers=arch)
    assert r.status_code == 201, r.text
    proj = r.json()["project"]
    src = next(s for s in proj["systems"] if s["role"] == "SOURCE")
    tgt = next(s for s in proj["systems"] if s["role"] == "TARGET")
    assert r.json()["import_counts"]["BKPF"] > 100
    # 2. discovery: company codes and plants
    r = client.post(f"{API}/systems/{src['id']}/discover", headers=arch)
    assert r.status_code == 200, r.text
    summ = r.json()["summary"]
    assert summ["org_units"]["COMPANY_CODE"] == 6 and summ["org_units"]["PLANT"] == 9
    assert summ["complexity"]["band"] in ("LOW", "MEDIUM", "HIGH") and summ["s4_impacts"]
    org = client.get(f"{API}/systems/{src['id']}/org-structure", headers=viewer).json()
    assert org["tree"] and any(c["code"] == "5000" for a in org["tree"] for c in a["children"])
    assert client.get(f"{API}/systems/{src['id']}/table-statistics", headers=viewer).json()
    bos = client.get(f"{API}/systems/{src['id']}/business-objects", params={"object_type": "SD.SalesOrder", "bukrs": "5000"}, headers=viewer).json()
    assert bos["items"] and all(i["bukrs"] == "5000" for i in bos["items"])
    # 3. graph
    r = client.post(f"{API}/systems/{src['id']}/graph/build", headers=arch)
    assert r.status_code == 200 and r.json()["edges"] > 1000
    stats = client.get(f"{API}/systems/{src['id']}/graph/stats", headers=viewer).json()
    assert stats["edges_by_type"]["CROSS_COMPANY"] > 0
    node = client.get(f"{API}/systems/{src['id']}/graph/nodes", params={"node_type": "SD.SalesOrder", "limit": 1}, headers=viewer).json()[0]["id"]
    nb = client.get(f"{API}/systems/{src['id']}/graph/neighbourhood", params={"node": node, "depth": 2}, headers=viewer).json()
    assert nb["nodes"] and nb["edges"]
    tr = client.post(f"{API}/systems/{src['id']}/graph/traverse", json={"seeds": [node]}, headers=viewer).json()
    assert node in tr["included"] and tr["traces"]
    # 4. scope: select carve-out company code 5000, preview impact, create manifest
    defn = {"name": "api carve-out", "source_system_id": src["id"], "target_system_id": tgt["id"], "company_codes": ["5000"], "target_ownership": {"company_code_map": {"5000": "SP01"}, "plant_map": {"5010": "SP10", "5020": "SP20"}, "controlling_area_map": {"1000": "SP01"}}}
    prev = client.post(f"{API}/projects/{proj['id']}/scopes/evaluate", json=defn, headers=viewer)
    assert prev.status_code == 200 and prev.json()["impact"]["objects_total"] > 0
    assert client.post(f"{API}/projects/{proj['id']}/manifests", json=defn, headers=viewer).status_code == 403
    r = client.post(f"{API}/projects/{proj['id']}/manifests", json=defn, headers=arch)
    assert r.status_code == 201, r.text
    man = r.json()
    assert man["version"] == 1 and man["status"] == "DRAFT" and len(man["content_hash"]) == 64
    objs = client.get(f"{API}/manifests/{man['id']}/objects", params={"requires_approval": True}, headers=viewer).json()
    assert objs["pending_dispositions"] > 0
    # carve-out reports
    comp = client.get(f"{API}/manifests/{man['id']}/carveout/completeness", headers=viewer).json()
    assert comp["pending_approvals"]["count"] > 0 and comp["intercompany_balances"] and comp["detections"]
    res = client.get(f"{API}/manifests/{man['id']}/carveout/residual", headers=viewer).json()
    assert res["residual_cleanup_candidates"]["count"] > 0
    # approval blocked until dispositions
    r = client.post(f"{API}/manifests/{man['id']}/approve", json={}, headers=appr)
    assert r.status_code == 400 and "disposition" in r.json()["detail"]
    r = client.post(f"{API}/manifests/{man['id']}/dispositions", json={"all_pending": True, "decision": "TRANSFER", "comment": "business approved"}, headers=appr)
    assert r.status_code == 200 and r.json()["pending"] == 0
    assert client.post(f"{API}/manifests/{man['id']}/approve", json={}, headers=arch).status_code == 403
    r = client.post(f"{API}/manifests/{man['id']}/approve", json={"comment": "go"}, headers=appr)
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    # 5. rules: generate, validate, create, dry-run, approve
    gen = client.post(f"{API}/projects/{proj['id']}/rulesets/generate", params={"manifest_id": man["id"]}, headers=arch).json()["source_yaml"]
    assert yaml.safe_load(gen)["rules"]
    assert client.post(f"{API}/rulesets/validate", json={"source_yaml": gen}, headers=viewer).json()["ok"]
    rs = client.post(f"{API}/projects/{proj['id']}/rulesets", json={"source_yaml": gen}, headers=arch).json()
    dr = client.post(f"{API}/rulesets/{rs['id']}/dry-run", json={"system_id": src["id"], "bukrs": "5000", "tables": ["BKPF", "KNA1"]}, headers=viewer).json()
    assert dr["records"] > 0 and dr["changed"] > 0
    assert client.post(f"{API}/rulesets/{rs['id']}/approve", json={}, headers=arch).status_code == 403
    assert client.post(f"{API}/rulesets/{rs['id']}/approve", json={}, headers=appr).json()["status"] == "APPROVED"
    # 6. run (operator), report, reconciliation, evidence
    r = client.post(f"{API}/projects/{proj['id']}/runs", json={"manifest_id": man["id"], "ruleset_id": rs["id"], "mode": "PRODUCTION"}, headers=op)
    assert r.status_code == 409
    r = client.post(f"{API}/projects/{proj['id']}/runs", json={"manifest_id": man["id"], "ruleset_id": rs["id"]}, headers=op)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "COMPLETED" and run["reconciliation"] == "PASS"
    rep = client.get(f"{API}/runs/{run['id']}/report", headers=viewer).json()
    assert rep["reconciliation"]["overall"] == "PASS" and rep["evidence"]["files"]
    recon = client.get(f"{API}/runs/{run['id']}/reconciliation", params={"layer": "FINANCIAL"}, headers=viewer).json()
    assert recon and all(x["status"] == "PASS" for x in recon)
    ev = client.get(f"{API}/runs/{run['id']}/evidence", headers=aud).json()
    assert ev["approvals"] and ev["audit_events"]
    assert client.post(f"{API}/runs/{run['id']}/signoff", json={"kind": "BUSINESS", "decision": "APPROVED"}, headers=appr).json()["ok"]
    # 7. agents propose, approver decides
    for name, ctx in (("carveout_ownership", {"manifest_id": man["id"]}), ("reconciliation_explanation", {"run_id": run["id"]}), ("cutover_risk", {"manifest_id": man["id"]}), ("compliance", {"manifest_id": man["id"]}), ("transformation_mapping", {"manifest_id": man["id"]}), ("scope_recommendation", {"company_code": "5000"}), ("data_quality", {}), ("migration_performance", {}), ("landscape_discovery", {}), ("business_object_classification", {}), ("dependency_analysis", {"manifest_id": man["id"]}), ("documentation", {"run_id": run["id"]})):
        r = client.post(f"{API}/projects/{proj['id']}/agents/{name}/run", json={"context": ctx}, headers=arch)
        assert r.status_code == 200, (name, r.text)
        d = r.json()
        assert 0 <= d["confidence"] <= 1 and d["status"] == "PROPOSED" and "authorize_production_migration" in d["proposal"]["forbidden_actions"]
    dec = client.get(f"{API}/projects/{proj['id']}/agents/decisions", headers=viewer).json()[0]
    assert client.post(f"{API}/agents/decisions/{dec['id']}/decide", json={"accept": True}, headers=arch).status_code == 403
    assert client.post(f"{API}/agents/decisions/{dec['id']}/decide", json={"accept": True}, headers=appr).json()["status"] == "ACCEPTED"
    # 8. cutover runbook, portfolio, audit
    rb = client.get(f"{API}/manifests/{man['id']}/cutover/runbook", headers=viewer).json()
    assert rb["critical_path"] and rb["forecast_downtime_minutes"] > 0 and rb["point_of_no_return"]
    assert any(p["id"] == proj["id"] and p["completed_runs"] >= 1 for p in client.get(f"{API}/platform/portfolio", headers=viewer).json())
    assert client.get(f"{API}/audit/verify", headers=aud).json()["ok"]
    caps = client.get(f"{API}/platform/capabilities", headers=viewer).json()
    assert any(c["status"] == "UNSUPPORTED" for c in caps)


def test_record_masking_for_viewer(client, tokens):
    r = client.post(f"{API}/projects/demo", json={"scale": 1, "seed": 5, "name": "mask"}, headers=tokens["architect"])
    src = next(s for s in r.json()["project"]["systems"] if s["role"] == "SOURCE")
    assert client.get(f"{API}/systems/{src['id']}/records", params={"table": "KNA1"}, headers=tokens["viewer"]).status_code == 403
    masked = client.get(f"{API}/systems/{src['id']}/records", params={"table": "KNA1", "limit": 3}, headers=tokens["auditor"]).json()
    assert masked["masked"] and all(row["NAME1"].startswith("tok_") for row in masked["rows"])
    clear = client.get(f"{API}/systems/{src['id']}/records", params={"table": "KNA1", "limit": 3}, headers=tokens["architect"]).json()
    assert not clear["masked"] and not clear["rows"][0]["NAME1"].startswith("tok_")
