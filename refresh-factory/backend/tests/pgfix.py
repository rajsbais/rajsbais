"""A real PostgreSQL for the multi-instance tests.

Uses RFACTORY_TEST_DATABASE_URL (an admin connection, e.g. CI's service container) when set; otherwise starts a throw-away cluster with
initdb/pg_ctl from the installed PostgreSQL (as the `postgres` user when running as root). Skips when neither is available."""
from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import tempfile
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")


def _bindir() -> str | None:
    for d in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if os.path.exists(os.path.join(d, "initdb")):
            return d
    w = shutil.which("initdb")
    return os.path.dirname(w) if w else None


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_admin_url():
    env = os.environ.get("RFACTORY_TEST_DATABASE_URL")
    if env:
        yield env
        return
    bindir = _bindir()
    if not bindir:
        pytest.skip("no PostgreSQL available (set RFACTORY_TEST_DATABASE_URL or install the server binaries)")
    root = os.geteuid() == 0
    base = tempfile.mkdtemp(prefix="rf-pg-")
    os.chmod(base, 0o755)
    data, port = os.path.join(base, "data"), _port()
    run = (lambda cmd: ["su", "postgres", "-c", cmd]) if root else (lambda cmd: ["sh", "-c", cmd])
    if root:
        shutil.chown(base, "postgres")
    try:
        subprocess.run(run(f"{bindir}/initdb -D {data} -A trust -U postgres >/dev/null"), check=True, capture_output=True)
        subprocess.run(run(f"{bindir}/pg_ctl -D {data} -o '-p {port} -k {base} -c listen_addresses=127.0.0.1 -c fsync=off' -l {base}/log -w start"), check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        pytest.skip(f"could not start a PostgreSQL cluster: {getattr(e, 'stderr', e)!r}")
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(run(f"{bindir}/pg_ctl -D {data} -m immediate stop"), capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def pgdb(pg_admin_url):
    """A fresh empty database; yields its URL."""
    from psycopg.conninfo import make_conninfo
    name = "rf_" + uuid.uuid4().hex[:10]
    with psycopg.connect(pg_admin_url, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(pg_admin_url, dbname=name)
    yield url
    with psycopg.connect(pg_admin_url, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
