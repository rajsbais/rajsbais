"""The tracker of pending work: one source, consistent, served by the API and written to docs."""
import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rfactory import roadmap as R
from rfactory.api.main import create_app

DOC = Path(__file__).resolve().parents[2] / "docs" / "07-roadmap-tracker.md"


def test_the_tracker_is_consistent():
    assert R.validate() == []
    ids = [i["id"] for i in R.ITEMS]
    assert len(ids) == len(set(ids)) >= 20
    assert {"R1", "R2", "R5", "B1", "B14"} <= set(ids)


@pytest.mark.parametrize("mutate,expect", [
    (lambda items: items[0].update(phase="PX"), "unknown phase"),
    (lambda items: items[0].update(status="soon"), "unknown status"),
    (lambda items: items[0].update(owner="them"), "unknown owner"),
    (lambda items: items[0].update(size="XL"), "unknown size"),
    (lambda items: items[0].update(after=["NOPE"]), "unknown NOPE"),
    (lambda items: items[0].update(after=[items[0]["id"]]), "waits on itself"),
    (lambda items: items[0].update(steps=[]), "needs steps"),
    (lambda items: items[0].update(status="after", after=[]), "nothing to wait on"),
    (lambda items: items.append(dict(items[0])), "duplicate ids"),
    (lambda items: (items[0].update(after=[items[1]["id"]]), items[1].update(after=[items[0]["id"]])), "cycle"),
])
def test_validation_catches_mistakes(monkeypatch, mutate, expect):
    items = copy.deepcopy(R.ITEMS)
    mutate(items)
    monkeypatch.setattr(R, "ITEMS", items)
    assert any(expect in p for p in R.validate()), R.validate()


def test_every_phase_has_work_and_nothing_waits_on_a_later_phase():
    order = [p[0] for p in R.PHASES]
    phase = {i["id"]: i["phase"] for i in R.ITEMS}
    assert all(any(i["phase"] == p for i in R.ITEMS) for p in order)
    for i in R.ITEMS:
        for a in i["after"]:
            assert order.index(phase[a]) <= order.index(phase[i["id"]]), f"{i['id']} waits on {a}, which is planned later"


def test_things_that_need_a_real_system_never_claim_to_be_ready():
    for i in R.ITEMS:
        if i["area"] == "Real systems":
            assert i["status"] != "ready", i["id"]
    assert next(i for i in R.ITEMS if i["id"] == "B1")["status"] == "declined"  # the user said: do not delete any data


def test_the_document_is_generated_from_the_list():
    assert DOC.read_text() == R.markdown(), "docs/07-roadmap-tracker.md is out of date: python -m rfactory.roadmap --write docs/07-roadmap-tracker.md"
    for i in R.ITEMS:
        assert f"### {i['id']} · {i['title']}" in R.markdown()


def test_api_serves_it_to_anyone_who_may_view(tmp_path):
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/roadmap").status_code == 401
    r = c.get("/api/roadmap", headers={"X-Demo-User": "erin.auditor"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == len(R.ITEMS) and [p["id"] for p in body["phases"]] == ["P1", "P2", "P3", "P4"]
    assert body["summary"]["total"] == len(R.ITEMS) and sum(body["summary"]["by_status"].values()) == len(R.ITEMS)
