"""Scenario matrix across several manifests: counts side by side, identical and differing objects, pairwise
deltas; the route with ids, defaults, limits and errors."""

API = "/api/v1"


def test_scenario_matrix(client, tokens, slice_result):
    pid = slice_result["project_id"]
    base = client.get(f"{API}/manifests/{slice_result['manifest_id']}", headers=tokens["architect"]).json()
    defn = {k: base["definition"][k] for k in ("name", "scenario_type", "carve_out_direction", "source_system_id", "target_system_id", "company_codes", "plants", "document_status", "historical_policy", "shared_object_policy", "cross_company_policy", "object_types_exclude", "target_ownership")}
    alt = client.post(f"{API}/projects/{pid}/manifests", json={**defn, "name": "scenario: shared referenced", "shared_object_policy": "REFERENCE", "cross_company_policy": "EXCLUDE"}, headers=tokens["architect"])
    assert alt.status_code == 201, alt.text
    r = client.get(f"{API}/projects/{pid}/manifests/matrix", params={"ids": f"{slice_result['manifest_id']},{alt.json()['id']}"}, headers=tokens["viewer"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert [m["id"] for m in d["manifests"]] == [slice_result["manifest_id"], alt.json()["id"]] and d["manifests"][1]["shared_object_policy"] == "REFERENCE"
    assert "FULLY_TRANSFERRED" in d["classifications"] and all(set(v) == {m["id"] for m in d["manifests"]} for v in d["matrix"].values())
    for m in d["manifests"]:
        assert sum(d["matrix"][k][m["id"]] for k in d["classifications"]) == m["objects"]
    assert d["objects_differing"] > 0 and d["objects_identical"] > 0 and d["objects_in_any"] >= d["objects_identical"] + d["objects_differing"]
    diff = d["differing"][0]
    assert diff["node"] and diff["type"] and diff[slice_result["manifest_id"]] != diff[alt.json()["id"]]
    assert len(d["pairwise"]) == 1 and d["pairwise"][0]["reclassified"] > 0
    # defaults: every manifest of the project, newest first, at most 8
    d2 = client.get(f"{API}/projects/{pid}/manifests/matrix", headers=tokens["viewer"]).json()
    assert d2["manifests"][0]["id"] == alt.json()["id"] and len(d2["manifests"]) <= 8 and len(d2["pairwise"]) == len(d2["manifests"]) * (len(d2["manifests"]) - 1) // 2
    assert client.get(f"{API}/projects/{pid}/manifests/matrix", params={"ids": "nope"}, headers=tokens["viewer"]).status_code == 404
    assert client.get(f"{API}/projects/{pid}/manifests/matrix", params={"ids": ",".join([slice_result["manifest_id"]] * 9)}, headers=tokens["viewer"]).status_code == 200  # duplicates collapse
