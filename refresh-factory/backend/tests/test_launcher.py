"""The one-command launcher (start.py): its checks, its choices, and a real start/stop of the platform."""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("keystone_start", ROOT / "start.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def busy_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    return s, s.getsockname()[1]


def test_python_and_node_checks(monkeypatch):
    assert S.python_ok((3, 13, 1)) and S.python_ok((3, 11)) and not S.python_ok((3, 10, 9)) and not S.python_ok((2, 7))
    monkeypatch.setattr(S.shutil, "which", lambda n: None)
    assert S.node_version() is None
    monkeypatch.setattr(S.shutil, "which", lambda n: "/usr/bin/node")
    fake = lambda out: (lambda *a, **k: type("R", (), {"stdout": out})())
    assert S.node_version(fake("v22.4.1\n")) == 22 and S.node_version(fake("v18.0.0")) == 18 and S.node_version(fake("garbage")) is None

    def boom(*a, **k):
        raise OSError("cannot run")
    assert S.node_version(boom) is None


def test_free_port_skips_8000_and_ports_in_use():
    s, p = busy_port()
    try:
        got = S.free_port(p, p + 5)
        assert got != p and p < got <= p + 5
        with pytest.raises(S.LauncherError, match="no free port"):
            S.free_port(p, p)
    finally:
        s.close()
    assert S.free_port(8000, 8001) == 8001  # 8000 is never offered: an SAP system on this PC may use it
    assert S.free_port() != 8000 and 8088 <= S.free_port() <= 8120


def test_ui_staleness(tmp_path):
    (tmp_path / "src").mkdir()
    src = tmp_path / "src" / "App.tsx"
    src.write_text("x")
    dist = tmp_path / "dist" / "index.html"
    assert S.ui_stale(dist, tmp_path)  # never built
    dist.parent.mkdir()
    dist.write_text("<html>")
    now = time.time()
    os.utime(src, (now - 100, now - 100))
    os.utime(dist, (now, now))
    assert not S.ui_stale(dist, tmp_path)  # built after the last edit
    os.utime(src, (now + 50, now + 50))
    assert S.ui_stale(dist, tmp_path)  # a source file is newer than the build
    (tmp_path / "src" / "deep").mkdir()
    deep = tmp_path / "src" / "deep" / "View.tsx"
    deep.write_text("y")
    os.utime(src, (now - 100, now - 100))
    os.utime(deep, (now + 50, now + 50))
    assert S.ui_stale(dist, tmp_path)  # nested sources count too


def test_server_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "STATE", tmp_path / "state")
    env = S.server_env(False, {"PATH": "x"})
    assert env["RFACTORY_DATA_DIR"] == str(tmp_path / "state" / "data") and (tmp_path / "state" / "data").is_dir()
    keep = S.server_env(False, {"RFACTORY_DATA_DIR": "/somewhere"})
    assert keep["RFACTORY_DATA_DIR"] == "/somewhere"  # an operator's choice is never overridden
    pg = S.server_env(False, {"RFACTORY_DATABASE_URL": "postgresql://x/y"})
    assert "RFACTORY_DATA_DIR" not in pg and pg["RFACTORY_DATABASE_URL"] == "postgresql://x/y"
    mem = S.server_env(True, {"RFACTORY_DATA_DIR": "/somewhere", "RFACTORY_DATABASE_URL": "postgresql://x/y", "OTHER": "1"})
    assert "RFACTORY_DATA_DIR" not in mem and "RFACTORY_DATABASE_URL" not in mem and mem["OTHER"] == "1"  # a throw-away run touches nothing durable


def test_the_server_listens_on_this_machine_only():
    cmd = S.server_command("python", 8099)
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1" and "0.0.0.0" not in cmd and cmd[cmd.index("--port") + 1] == "8099"


def test_unsafe_ports_and_old_pythons_are_refused(capsys, monkeypatch):
    for bad in (["--port", "8000"], ["--port", "80"], ["--port", "70000"]):
        assert S.main(bad + ["--no-venv", "--no-build", "--memory", "--no-browser"]) == 1
    assert "not 8000" in capsys.readouterr().err
    monkeypatch.setattr(S, "python_ok", lambda *a: False)
    assert S.main(["--no-venv"]) == 1 and "3.11 or newer" in capsys.readouterr().err


def test_check_changes_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(S, "STATE", tmp_path / "state")
    monkeypatch.setattr(S, "VENV", tmp_path / "venv")
    assert S.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "Python" in out and "port:" in out and "UI build" in out
    assert not (tmp_path / "state").exists() and not (tmp_path / "venv").exists()


def test_without_a_venv_the_current_python_is_used_and_nothing_is_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "VENV", tmp_path / "venv")
    monkeypatch.setattr(S, "STATE", tmp_path / "state")
    assert S.ensure_backend(False) == sys.executable and not (tmp_path / "venv").exists() and not (tmp_path / "state").exists()


def test_a_missing_node_is_explained_and_the_api_still_runs(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(S, "node_version", lambda *a: None)
    monkeypatch.setattr(S, "ui_stale", lambda *a: True)
    monkeypatch.setattr(S, "FRONTEND", tmp_path)
    assert S.ensure_ui(False) is False  # no build possible, none present
    assert "Node.js 20 or newer" in capsys.readouterr().out
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("old")
    assert S.ensure_ui(False) is True  # an older build is better than none


def launch(port, env_extra, *flags):
    env = {**os.environ, **env_extra}
    for k in ("RFACTORY_DATA_DIR", "RFACTORY_DATABASE_URL"):
        env.pop(k, None) if k not in env_extra else None
    return subprocess.Popen([sys.executable, str(ROOT / "start.py"), "--no-venv", "--no-build", "--no-browser", "--port", str(port), *flags],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def get(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


def up(url, proc, seconds=60):
    for _ in range(int(seconds * 4)):
        if proc.poll() is not None:
            return False
        try:
            return get(url + "/api/health")["status"] == "ok"
        except OSError:
            time.sleep(0.25)
    return False


def stop(proc):
    proc.terminate()  # SIGTERM: the launcher must stop the platform behind it, not leave it running
    try:
        out, _ = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    return out


def refused(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) != 0


def test_it_really_starts_and_stops_the_platform_and_keeps_state_between_runs(tmp_path):
    s0, port = busy_port()
    s0.close()
    home = {"KEYSTONE_HOME": str(tmp_path / "home")}
    url = f"http://127.0.0.1:{port}"
    p = launch(port, home)
    try:
        assert up(url, p), "the platform did not start"
        root = {"X-Demo-User": "root.admin", "Content-Type": "application/json"}
        get(url + "/api/demo/bootstrap", root, b"{}")
        assert len(get(url + "/api/systems", {"X-Demo-User": "alice.basis"})) == 4
    finally:
        out = stop(p)
    assert "stopping" in out  # the launcher handled the stop itself (it may or may not have printed "ready" yet: the test polls independently)
    assert refused(port), "the platform was left running after the launcher stopped"
    assert (tmp_path / "home" / "data" / "state.db").exists()
    p2 = launch(port, home)
    try:
        assert up(url, p2)
        assert len(get(url + "/api/systems", {"X-Demo-User": "alice.basis"})) == 4  # state survived the stop
    finally:
        stop(p2)
    assert refused(port)


def test_a_memory_run_keeps_nothing(tmp_path):
    s0, port = busy_port()
    s0.close()
    p = launch(port, {"KEYSTONE_HOME": str(tmp_path / "home")}, "--memory")
    try:
        assert up(f"http://127.0.0.1:{port}", p)
        assert get(f"http://127.0.0.1:{port}/api/persistence/status", {"X-Demo-User": "alice.basis"})["durable"] is False
    finally:
        stop(p)
    assert not (tmp_path / "home").exists()


def test_the_launcher_contains_no_delete_of_user_data():
    src = (ROOT / "start.py").read_text()
    for forbidden in ("rmtree", "os.remove", ".unlink(", "os.unlink", "shutil.rmtree", "DROP "):
        assert forbidden not in src, forbidden


def test_wrappers_exist_and_call_the_launcher():
    assert "start.py" in (ROOT / "start.sh").read_text() and "start.py" in (ROOT / "start.bat").read_text()
    assert os.access(ROOT / "start.sh", os.X_OK)
