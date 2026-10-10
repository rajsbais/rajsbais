#!/usr/bin/env python3
"""One command to run Keystone (SAP Intelligent Refresh Factory) on this machine.

    python start.py                 first run: creates a virtual environment, installs the backend, builds the UI, starts the platform, opens the browser
    python start.py --memory        a throw-away run: nothing is kept after you stop it
    python start.py --check         only check that everything needed is installed, change nothing

What it does, in order: checks Python (3.11 or newer) and, for the UI, Node.js (20 or newer); creates `.venv` and installs the backend into it (once, again
when `backend/pyproject.toml` changes); installs the UI packages and builds it (once, again when a UI source file is newer than the build); starts the API
and UI together on the first free port from 8088 (never 8000: an SAP system on this PC may use it); waits until it answers; opens your browser.

State is kept encrypted in `.rfactory/data` (or under $KEYSTONE_HOME; created on first start, with its keys next to it: development-grade custody, see docs/06-deployment.md).
This launcher never deletes anything. Stop with Ctrl+C.

It does not connect to SAP, install SAP libraries, or change any system outside this folder. Everything it shows is simulated until you register a real
system yourself (docs/04-real-system-test.md).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND, FRONTEND = ROOT / "backend", ROOT / "frontend"
STATE = Path(os.environ.get("KEYSTONE_HOME") or ROOT / ".rfactory")
VENV = ROOT / ".venv"
MIN_PY, MIN_NODE = (3, 11), 20
FIRST_PORT, LAST_PORT = 8088, 8120


class LauncherError(RuntimeError):
    pass


def say(msg: str) -> None:
    print(f"[keystone] {msg}", flush=True)


# ---------------------------------------------------------------------------------------------------------------- checks
def python_ok(version=None) -> bool:
    return tuple((version or sys.version_info)[:2]) >= MIN_PY


def node_version(run=subprocess.run) -> int | None:
    """The major version of the installed Node.js, or None when it is missing or unreadable."""
    exe = shutil.which("node")
    if not exe:
        return None
    try:
        out = run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout.strip().lstrip("v")
        return int(out.split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def free_port(first: int = FIRST_PORT, last: int = LAST_PORT, avoid=(8000,)) -> int:
    for port in range(first, last + 1):
        if port in avoid:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise LauncherError(f"no free port between {first} and {last}: close something or choose one with --port")


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def digest(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.read_bytes() if p.exists() else b"")
    return h.hexdigest()


def ui_stale(dist: Path | None = None, src: Path | None = None) -> bool:
    """True when the UI has never been built or a source file is newer than the build."""
    dist = dist or FRONTEND / "dist" / "index.html"
    src = src or FRONTEND
    if not dist.exists():
        return True
    built = dist.stat().st_mtime
    watched = [p for pat in ("src/**/*", "index.html", "package.json", "vite.config.ts") for p in src.glob(pat) if p.is_file()]
    return any(p.stat().st_mtime > built for p in watched)


# ---------------------------------------------------------------------------------------------------------------- steps
def run(cmd: list[str], cwd: Path | None = None, what: str = "") -> None:
    say(what or " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd)
    if r.returncode != 0:
        raise LauncherError(f"failed: {' '.join(cmd)} (exit {r.returncode})")


def ensure_backend(use_venv: bool) -> str:
    """Install the backend once (and again when its dependencies change). Returns the Python to run it with."""
    if not use_venv:
        return sys.executable
    py = venv_python()
    if not py.exists():
        run([sys.executable, "-m", "venv", str(VENV)], what="creating the virtual environment (.venv)")
    marker = STATE / "backend.sha"
    want = digest(BACKEND / "pyproject.toml")
    if not marker.exists() or marker.read_text().strip() != want:
        run([str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-e", f"{BACKEND}[postgres]"], what="installing the backend (this takes a minute the first time)")
        STATE.mkdir(exist_ok=True)
        marker.write_text(want)
    return str(py)


def ensure_ui(rebuild: bool) -> bool:
    """Build the UI when needed. Returns False (and says why) when it cannot be built: the API still runs, without the screens."""
    if not rebuild and not ui_stale():
        return True
    major = node_version()
    if major is None or major < MIN_NODE:
        say(f"Node.js {MIN_NODE} or newer is needed to build the UI and was not found: https://nodejs.org/ . Starting the API without the screens (its documentation is at /docs).")
        return (FRONTEND / "dist" / "index.html").exists()
    npm = shutil.which("npm") or "npm"
    if not (FRONTEND / "node_modules").exists():
        run([npm, "ci" if (FRONTEND / "package-lock.json").exists() else "install"], cwd=FRONTEND, what="installing the UI packages (once)")
    run([npm, "run", "build"], cwd=FRONTEND, what="building the UI")
    return True


def wait_healthy(url: str, proc: subprocess.Popen, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=2) as r:
                if r.status == 200 and json.loads(r.read()).get("status") == "ok":
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(0.4)
    return False


def server_command(python: str, port: int) -> list[str]:
    return [python, "-m", "uvicorn", "rfactory.api.main:app", "--host", "127.0.0.1", "--port", str(port)]


def server_env(memory: bool, base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    if memory:
        env.pop("RFACTORY_DATA_DIR", None)
        env.pop("RFACTORY_DATABASE_URL", None)
    elif not env.get("RFACTORY_DATABASE_URL") and not env.get("RFACTORY_DATA_DIR"):
        data = STATE / "data"
        data.mkdir(parents=True, exist_ok=True)
        env["RFACTORY_DATA_DIR"] = str(data)
    return env


def check_only(use_venv: bool) -> int:
    ok = True
    say(f"Python {sys.version.split()[0]}: " + ("ok" if python_ok() else f"too old, {MIN_PY[0]}.{MIN_PY[1]} or newer is needed"))
    ok &= python_ok()
    n = node_version()
    say("Node.js: " + (f"{n}, ok" if n and n >= MIN_NODE else f"missing or older than {MIN_NODE}: the UI cannot be built (the API can still run)"))
    say("virtual environment: " + ("present" if venv_python().exists() else "will be created on the first start") if use_venv else "virtual environment: not used")
    say("UI build: " + ("current" if not ui_stale() else "will be built on the next start"))
    try:
        say(f"port: {free_port()} is free")
    except LauncherError as e:
        say(str(e))
        ok = False
    say(f"state folder: {STATE / 'data'} (" + ("exists" if (STATE / 'data').exists() else "created on the first start") + ")")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, help="port to listen on (default: the first free one from 8088)")
    ap.add_argument("--memory", action="store_true", help="keep nothing: state is lost when you stop")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    ap.add_argument("--no-venv", action="store_true", help="use this Python as it is instead of creating .venv (you installed the backend yourself)")
    ap.add_argument("--no-build", action="store_true", help="do not build the UI even if it is out of date")
    ap.add_argument("--rebuild", action="store_true", help="build the UI again even if it looks current")
    ap.add_argument("--check", action="store_true", help="only check the prerequisites; change nothing")
    a = ap.parse_args(argv)
    try:
        if not python_ok():
            raise LauncherError(f"Python {MIN_PY[0]}.{MIN_PY[1]} or newer is needed; this is {sys.version.split()[0]}")
        if a.check:
            return check_only(not a.no_venv)
        if a.port is not None and not (1024 <= a.port <= 65535) or a.port == 8000:
            raise LauncherError("choose a port between 1024 and 65535, and not 8000 (an SAP system on this PC may use it)")
        python = ensure_backend(not a.no_venv)
        ui = True if a.no_build else ensure_ui(a.rebuild)
        port = a.port or free_port()
    except LauncherError as e:
        print(f"[keystone] ERROR: {e}", file=sys.stderr)
        return 1
    env = server_env(a.memory)
    url = f"http://127.0.0.1:{port}"
    say(("throw-away run: nothing will be kept" if a.memory else f"state is kept in {env.get('RFACTORY_DATA_DIR') or 'the PostgreSQL database'}") + "; all SAP data is simulated")
    proc = subprocess.Popen(server_command(python, port), cwd=BACKEND, env=env)
    if hasattr(signal, "SIGTERM"):  # `kill`, a service manager or Docker: stop the platform too, never leave it running unattended
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, signal.default_int_handler)  # also when started from a script that ignores Ctrl+C
    try:
        if not wait_healthy(url, proc):
            print("[keystone] ERROR: the platform did not start (see the messages above)", file=sys.stderr)
            proc.terminate()
            return 1
        say(f"ready: {url}" + ("" if ui else "  (no screens: Node.js missing, API documentation at /docs)"))
        say("sign in from the sidebar (demo users); start with Control tower > Load synthetic landscape, then Guided refresh. Stop with Ctrl+C.")
        if not a.no_browser:
            threading.Timer(0.2, lambda: webbrowser.open(url)).start()
        return proc.wait()
    except KeyboardInterrupt:
        say("stopping")
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
