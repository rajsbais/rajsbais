"""Durable platform state (SIMULATED SAP stays simulated; the *platform's* state becomes durable).

Design
  * State is split into aggregates (a system with its simulated adapter, a project with its plan and masking engine, a run, a delta
    scenario, a dataset, a full-refresh program, the orchestration queue ...). Each is serialised, hashed, and written only when it changed.
  * One SQLite transaction per save: either every changed aggregate is stored or none is. Saves happen at REQUEST BOUNDARIES (the API
    serialises mutating requests), so the stored state is always consistent; a crash in the middle of a request loses that request's effects
    as a whole. The hash-chained audit log is appended immediately and is not rolled back, so after such a crash it can show an attempt that
    left no state behind: the audit log records what was tried, the store records what is true.
  * Every blob is encrypted and authenticated with Fernet before it touches disk (state includes masking keys, token vault entries and the
    pre-refresh target backups). Blobs are unpickled only after authentication and through an allow-listed unpickler.
  * Envelope encryption: blobs are encrypted with a random data key (DEK); the DEK is stored wrapped by a key-encryption key (KEK) from a
    `KeyProvider`. Rotating the KEK re-wraps one small value; rotating the DEK re-encrypts every blob in one transaction. A KMS/HSM provider
    only has to implement wrap/unwrap (no such provider is built; `LocalKek` is the only one).
  * Key custody is the weak point: the KEK comes from RFACTORY_STATE_KEY, otherwise a 0600 key file next to the database (development only).

NOT provided: PostgreSQL (the schema in db/schema.sql is still a design), multi-process writers, schema migrations (a version mismatch is
refused), online backup, retention of old versions.
"""
from __future__ import annotations

import hashlib
import io
import os
import pickle
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SCHEMA_VERSION = 2
KEY_CHECK = b"rfactory-state-key-check"


class StoreError(RuntimeError):
    pass


class KeyProvider:
    """Wraps and unwraps the data key. A KMS adapter (AWS KMS, Azure Key Vault, an HSM) would implement exactly these two methods."""
    name = "abstract"

    def wrap(self, dek: bytes) -> bytes:
        raise NotImplementedError

    def unwrap(self, wrapped: bytes) -> bytes:
        raise NotImplementedError


class LocalKek(KeyProvider):
    """A Fernet key held by this process (from the environment or a key file). Development-grade custody."""
    name = "local-fernet-kek"

    def __init__(self, kek: bytes):
        self._f = Fernet(kek)

    def wrap(self, dek: bytes) -> bytes:
        return self._f.encrypt(dek)

    def unwrap(self, wrapped: bytes) -> bytes:
        try:
            return self._f.decrypt(wrapped)
        except InvalidToken:
            raise StoreError("the state key does not match this database: set RFACTORY_STATE_KEY to the key it was created with")


_SAFE_BUILTINS = {"set", "frozenset", "dict", "list", "tuple", "str", "int", "float", "bool", "bytes", "bytearray", "complex", "slice", "range", "object"}
_SAFE_PREFIXES = ("rfactory.", "pydantic", "datetime", "collections", "decimal", "enum", "copyreg", "uuid", "cryptography.fernet", "cryptography.hazmat.primitives", "cryptography.hazmat.bindings", "_cffi_backend", "typing", "zoneinfo")


class _Unpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str):
        if module == "builtins" and name in _SAFE_BUILTINS:
            return super().find_class(module, name)
        if module != "builtins" and (module + ".").startswith(_SAFE_PREFIXES) or module.startswith(_SAFE_PREFIXES):
            return super().find_class(module, name)
        raise StoreError(f"refusing to load {module}.{name}: not in the allow-list")


def _dumps(obj) -> bytes:
    return pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)


def _loads(data: bytes):
    return _Unpickler(io.BytesIO(data)).load()


def collect(svc) -> dict[tuple[str, str], object]:
    """Every aggregate the platform holds, keyed by (kind, id). Objects inside one aggregate keep their mutual references."""
    out: dict[tuple[str, str], object] = {}
    for sid, s in svc.systems.items():
        if svc.is_local(sid):
            out[("system", sid)] = {"system": s, "adapter": svc.adapters[sid]}
        else:  # a remote source: only the profile (secret references, no secrets) is stored; the connection is re-made on start
            out[("system", sid)] = {"system": s, "adapter": None, "profile": svc.remote_profiles.get(sid)}
    for pid, p in svc.projects.items():
        out[("project", pid)] = {"project": p, "sensitive": svc.required_sensitive.get(pid), "engine": svc.engines.get(pid)}
    for rid, r in svc.runs.items():
        out[("run", rid)] = r
    for kind, mapping in (("delta", svc.delta.scenarios), ("tdm_policy", svc.tdm.policies), ("tdm_dataset", svc.tdm.datasets), ("tdm_request", svc.tdm.requests),
                          ("lean_template", svc.lean.templates), ("lean_build", svc.lean.builds), ("pc_profile", svc.postcopy.profiles), ("pc_run", svc.postcopy.runs),
                          ("full", svc.full.programs), ("job", svc.orch.jobs), ("schedule", svc.orch.schedules), ("pipeline", svc.orch.pipelines)):
        for k, v in mapping.items():
            out[(kind, k)] = v
    out[("agents", "all")] = {"reports": svc.agents.reports, "recs": svc.agents.recs}
    out[("revoke", "all")] = {"jtis": svc.revocations.jtis, "subjects": svc.revocations.subjects}
    out[("writereq", "all")] = dict(svc.write_requests)
    out[("bench", "all")] = {"samples": svc.bench.samples, "accuracy": svc.bench.accuracy, "bindings": svc.bench.bindings}
    o = svc.orch
    out[("orch", "state")] = {"windows": o.windows, "leases": o.leases, "events": o.events, "subs": o.subs, "outbox": o.outbox, "skew": o.skew}
    return out


def apply(svc, objs: dict[tuple[str, str], object]) -> None:
    by: dict[str, dict] = {}
    for (kind, k), v in objs.items():
        by.setdefault(kind, {})[k] = v
    for sid, pack in by.get("system", {}).items():
        svc.systems[sid] = pack["system"]
        if pack["adapter"] is None:
            svc.remote_profiles[sid] = pack["profile"]
            svc.adapters[sid] = svc.rebuild_remote(pack["system"], pack["profile"])
        else:
            svc.adapters[sid] = pack["adapter"]
            pack["adapter"].system = pack["system"]
    for pid, pack in by.get("project", {}).items():
        svc.projects[pid] = pack["project"]
        if pack["sensitive"] is not None:
            svc.required_sensitive[pid] = pack["sensitive"]
        if pack["engine"] is not None:
            svc.engines[pid] = pack["engine"]
    svc.runs.update(by.get("run", {}))
    for kind, mapping in (("delta", svc.delta.scenarios), ("tdm_policy", svc.tdm.policies), ("tdm_dataset", svc.tdm.datasets), ("tdm_request", svc.tdm.requests),
                          ("lean_template", svc.lean.templates), ("lean_build", svc.lean.builds), ("pc_profile", svc.postcopy.profiles), ("pc_run", svc.postcopy.runs),
                          ("full", svc.full.programs), ("job", svc.orch.jobs), ("schedule", svc.orch.schedules), ("pipeline", svc.orch.pipelines)):
        mapping.update(by.get(kind, {}))
    if "agents" in by:
        a = by["agents"]["all"]
        svc.agents.reports, svc.agents.recs = a["reports"], a["recs"]
    if "revoke" in by:
        svc.revocations.jtis, svc.revocations.subjects = by["revoke"]["all"]["jtis"], by["revoke"]["all"]["subjects"]
    if "writereq" in by:
        svc.write_requests = dict(by["writereq"]["all"])
    if "bench" in by:
        b = by["bench"]["all"]
        svc.bench.samples, svc.bench.accuracy, svc.bench.bindings = b["samples"], b["accuracy"], b["bindings"]
    if "orch" in by:
        s = by["orch"]["state"]
        o = svc.orch
        o.windows, o.leases, o.events, o.subs, o.outbox, o.skew = s["windows"], s["leases"], s["events"], s["subs"], s["outbox"], s["skew"]


class StateStore:
    def __init__(self, svc, path: Path, key: bytes | None = None, provider: KeyProvider | None = None):
        self.svc, self.path = svc, Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key_source = "environment (RFACTORY_STATE_KEY)" if (key or os.environ.get("RFACTORY_STATE_KEY")) else "key file next to the database (development only)"
        self.provider = provider or LocalKek(self._resolve_key(key))
        self._fernet: Fernet | None = None  # the data key, known once the database is opened (load) or created
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS aggregates (kind TEXT NOT NULL, id TEXT NOT NULL, hash TEXT NOT NULL, blob BLOB NOT NULL, updated TEXT NOT NULL, PRIMARY KEY (kind, id))")
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
        self._hashes: dict[tuple[str, str], str] = {}
        self.last_saved: str | None = None
        self.last_save_stats: dict = {}
        self.interrupted: list[str] = []

    # ---------------------------------------------------------------- key handling
    def _resolve_key(self, key: bytes | None) -> bytes:
        if key:
            return key
        env = os.environ.get("RFACTORY_STATE_KEY")
        if env:
            return env.encode()
        kf = self.path.with_suffix(".key")
        if kf.exists():
            return kf.read_bytes().strip()
        k = Fernet.generate_key()
        fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(k)
        return k

    # ---------------------------------------------------------------- load
    def exists(self) -> bool:
        return self._db.execute("SELECT COUNT(*) FROM aggregates").fetchone()[0] > 0 or self._meta("schema") is not None

    def _meta(self, k: str):
        r = self._db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return r[0] if r else None

    def _init_meta(self) -> None:
        dek = Fernet.generate_key()
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION).encode(),))
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('wrapped_dek', ?)", (self.provider.wrap(dek),))
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('key_check', ?)", (Fernet(dek).encrypt(KEY_CHECK),))
        self._fernet = Fernet(dek)

    def _open_existing(self) -> None:
        ver = self._meta("schema")
        if int(ver) != SCHEMA_VERSION:
            raise StoreError(f"state schema version {int(ver)} is not supported by this build (expects {SCHEMA_VERSION}); no migration exists")
        wrapped = self._meta("wrapped_dek")
        if wrapped is None:
            raise StoreError("the database has no wrapped data key: refusing to load")
        dek = self.provider.unwrap(wrapped)  # StoreError when the KEK is wrong
        f = Fernet(dek)
        try:
            if f.decrypt(self._meta("key_check")) != KEY_CHECK:
                raise InvalidToken
        except InvalidToken:
            raise StoreError("the data key does not open this database: it was modified or corrupted")
        self._fernet = f

    def load(self) -> int:
        """Restore the platform from disk. Refuses (never silently starts empty) on a wrong key, tampering, or a schema mismatch."""
        with self._lock:
            ver = self._meta("schema")
            if ver is None:
                if self._db.execute("SELECT COUNT(*) FROM aggregates").fetchone()[0]:
                    raise StoreError("state database has data but no schema marker: refusing to load")
                self._init_meta()
                return 0
            self._open_existing()
            objs: dict[tuple[str, str], object] = {}
            for kind, k, h, blob in self._db.execute("SELECT kind, id, hash, blob FROM aggregates"):
                try:
                    plain = self._fernet.decrypt(blob)
                except InvalidToken:
                    raise StoreError(f"aggregate {kind}/{k} failed authentication: the database was modified or corrupted")
                if hashlib.sha256(plain).hexdigest() != h:
                    raise StoreError(f"aggregate {kind}/{k} does not match its recorded hash")
                objs[(kind, k)] = _loads(plain)
                self._hashes[(kind, k)] = h
            apply(self.svc, objs)
            self.interrupted = self.find_interrupted()
            if self.interrupted:
                self.svc.audit.append("system", "persistence.interrupted_work_found", "store", {"items": self.interrupted[:20]})
            return len(objs)

    def find_interrupted(self) -> list[str]:
        """Work recorded as in progress when the state was saved. Saves happen at request boundaries, so this means the process was
        stopped mid-request AFTER an explicit checkpoint, or a module left a status behind. It is reported, never silently 'fixed'."""
        svc, out = self.svc, []
        out += [f"project {k}" for k, p in svc.projects.items() if p.status == "RUNNING"]
        out += [f"run {k}" for k, r in svc.runs.items() if r.status == "RUNNING"]
        out += [f"delta {k}" for k, d in svc.delta.scenarios.items() if d.status == "RUNNING"]
        out += [f"full-refresh {k}" for k, p in svc.full.programs.items() if p.status == "RUNNING"]
        out += [f"post-copy {k}" for k, r in svc.postcopy.runs.items() if r.status == "RUNNING"]
        out += [f"job {k}" for k, j in svc.orch.jobs.items() if j.status == "RUNNING"]
        return out

    # ---------------------------------------------------------------- save
    def save(self) -> dict:
        """Write every aggregate that changed since the last save, in one transaction."""
        with self._lock:
            if self._fernet is None:
                raise StoreError("the store has not been opened: call load() first")
            now = datetime.now(timezone.utc).isoformat()
            cur = collect(self.svc)
            changed: list[tuple[tuple[str, str], str, bytes]] = []
            for key, obj in cur.items():
                plain = _dumps(obj)
                h = hashlib.sha256(plain).hexdigest()
                if self._hashes.get(key) != h:
                    changed.append((key, h, plain))
            gone = [k for k in self._hashes if k not in cur]
            with self._db:  # one transaction: all or nothing
                for (kind, k), h, plain in changed:
                    self._db.execute("INSERT OR REPLACE INTO aggregates VALUES (?,?,?,?,?)", (kind, k, h, self._fernet.encrypt(plain), now))
                for kind, k in gone:
                    self._db.execute("DELETE FROM aggregates WHERE kind=? AND id=?", (kind, k))
            for (key, h, _p) in changed:
                self._hashes[key] = h
            for k in gone:
                self._hashes.pop(k, None)
            self.last_saved = now
            self.last_save_stats = {"written": len(changed), "deleted": len(gone), "aggregates": len(cur)}
            return self.last_save_stats

    # ---------------------------------------------------------------- key rotation
    @classmethod
    def open_for_maintenance(cls, path: Path, kek: bytes) -> "StateStore":
        """Open a database without a running platform (offline key rotation)."""
        st = cls(None, path, key=kek)
        if st._meta("schema") is None:
            raise StoreError("no database to maintain")
        st._open_existing()
        return st

    def rotate_kek(self, new_provider: KeyProvider) -> None:
        """Re-wrap the data key under a new key-encryption key. Cheap: no blob is touched."""
        with self._lock:
            dek = self.provider.unwrap(self._meta("wrapped_dek"))
            with self._db:
                self._db.execute("INSERT OR REPLACE INTO meta VALUES ('wrapped_dek', ?)", (new_provider.wrap(dek),))
            self.provider = new_provider

    def rotate_dek(self) -> int:
        """Replace the data key and re-encrypt every stored blob in ONE transaction (all or nothing)."""
        with self._lock:
            old, new_key = self._fernet, Fernet.generate_key()
            new = Fernet(new_key)
            rows = self._db.execute("SELECT kind, id, blob FROM aggregates").fetchall()
            with self._db:
                for kind, k, blob in rows:
                    self._db.execute("UPDATE aggregates SET blob=? WHERE kind=? AND id=?", (new.encrypt(old.decrypt(blob)), kind, k))
                self._db.execute("INSERT OR REPLACE INTO meta VALUES ('wrapped_dek', ?)", (self.provider.wrap(new_key),))
                self._db.execute("INSERT OR REPLACE INTO meta VALUES ('key_check', ?)", (new.encrypt(KEY_CHECK),))
            self._fernet = new
            return len(rows)

    def status(self) -> dict:
        kinds: dict[str, int] = {}
        for kind, _ in self._hashes:
            kinds[kind] = kinds.get(kind, 0) + 1
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"durable": True, "database": self.path.name, "bytes": size, "aggregates": len(self._hashes), "by_kind": kinds, "schema_version": SCHEMA_VERSION,
                "interrupted_work": self.interrupted, "encrypted_at_rest": True, "key_source": self.key_source, "key_provider": self.provider.name, "envelope_encryption": True, "last_saved": self.last_saved, "last_save": self.last_save_stats,
                "limits": ["single writer: mutating API requests are serialised", "SQLite, not PostgreSQL", "no schema migrations", "no online backup or version history",
                           "the audit log (audit.jsonl) is append-only on disk and is not encrypted"]}
