import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture(scope="session")
def db_url(tmp_path_factory):
    d = tmp_path_factory.mktemp("sdtf")
    os.environ["SDTF_EVIDENCE_DIR"] = str(d / "evidence")
    return f"sqlite:///{d / 'test.db'}"


@pytest.fixture(scope="session")
def engine(db_url):
    from sdtf import config
    os.environ["SDTF_STAGING_DIR"] = os.path.join(os.path.dirname(os.environ["SDTF_EVIDENCE_DIR"]), "staging")
    config.settings = config.Settings(database_url=db_url, evidence_dir=os.environ["SDTF_EVIDENCE_DIR"], staging_dir=os.environ["SDTF_STAGING_DIR"])
    from sdtf.db import init_schema, reset_engine

    reset_engine(db_url)
    init_schema()
    yield


@pytest.fixture()
def session(engine):
    from sdtf.db import session_scope

    with session_scope() as s:
        yield s


@pytest.fixture(scope="session")
def slice_result(engine):
    """One full vertical slice shared by read-only tests."""
    from sdtf.db import session_scope
    from sdtf.demo import run_vertical_slice

    with session_scope() as s:
        out = run_vertical_slice(s, scale=1, seed=7)
        return {"project_id": out["project"].id, "source_id": out["source"].id, "target_id": out["target"].id, "manifest_id": out["manifest"].id, "ruleset_id": out["ruleset"].id, "run_id": out["run"].id, "graph_stats": out["graph_stats"], "run_status": out["run"].status, "report": out["run"].report}


@pytest.fixture(scope="session")
def client(engine):
    from fastapi.testclient import TestClient

    from sdtf.main import app

    with TestClient(app) as c:
        yield c


def login(client, username):
    r = client.post("/api/v1/auth/token", json={"username": username, "password": username})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="session")
def tokens(client):
    return {u: login(client, u) for u in ("admin", "architect", "approver", "operator", "auditor", "viewer")}
