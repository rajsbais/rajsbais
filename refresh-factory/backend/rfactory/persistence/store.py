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

Two backends (persistence/backends.py): SQLite (one process) and PostgreSQL (several instances sharing one database: a cross-instance write
lock, a version counter, catch-up on read and write, a shared hash-chained audit log). See that module for exactly what is and is not provided.

NOT provided: schema migrations (a version mismatch is refused), online backup, retention of old versions, horizontal write scalability.
"""
from __future__ import annotations

import hashlib
import io
import os
import pickle
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .backends import Backend, PostgresBackend, SqliteBackend

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


def _maps(svc) -> tuple[tuple[str, dict], ...]:
    return (("delta", svc.delta.scenarios), ("tdm_policy", svc.tdm.policies), ("tdm_dataset", svc.tdm.datasets), ("tdm_request", svc.tdm.requests),
            ("lean_template", svc.lean.templates), ("lean_build", svc.lean.builds), ("pc_profile", svc.postcopy.profiles), ("pc_run", svc.postcopy.runs),
            ("full", svc.full.programs), ("job", svc.orch.jobs), ("schedule", svc.orch.schedules), ("pipeline", svc.orch.pipelines))


def forget(svc, kind: str, k: str) -> None:
    """Drop an aggregate another instance deleted (the singletons are never deleted)."""
    if kind == "system":
        svc.systems.pop(k, None); svc.adapters.pop(k, None); svc.remote_profiles.pop(k, None)
    elif kind == "project":
        svc.projects.pop(k, None); svc.required_sensitive.pop(k, None); svc.engines.pop(k, None)
    elif kind == "run":
        svc.runs.pop(k, None)
    else:
        for name, mapping in _maps(svc):
            if name == kind:
                mapping.pop(k, None)


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
    for kind, mapping in _maps(svc):
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
    for kind, mapping in _maps(svc):
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
    def __init__(self, svc, path: Path | None = None, key: bytes | None = None, provider: KeyProvider | None = None, backend: Backend | None = None):
        if backend is None and path is None:
            raise StoreError("a state store needs a path (SQLite) or a backend")
        self.svc = svc
        self.backend = backend or SqliteBackend(Path(path))
        self.path = getattr(self.backend, "path", None)  # the database file, for SQLite only
        self.key_source = "environment (RFACTORY_STATE_KEY)" if (key or os.environ.get("RFACTORY_STATE_KEY")) else "key file next to the database (development only)"
        self.provider = provider or LocalKek(self._resolve_key(key))
        self._fernet: Fernet | None = None  # the data key, known once the database is opened (load) or created
        self._lock = threading.RLock()
        self._hashes: dict[tuple[str, str], str] = {}  # hash of what THIS process last serialised/loaded, per aggregate
        self._remote: dict[tuple[str, str], str] = {}  # hash the database held for it when we last looked
        self._version = -1
        self._write_held = False
        self.last_saved: str | None = None
        self.last_save_stats: dict = {}
        self.interrupted: list[str] = []
        self.refreshed = 0  # aggregates picked up from other instances, for status

    # ---------------------------------------------------------------- key handling
    def _resolve_key(self, key: bytes | None) -> bytes:
        if key:
            return key
        env = os.environ.get("RFACTORY_STATE_KEY")
        if env:
            return env.encode()
        if self.backend.multi_instance:
            raise StoreError("a shared database needs the state key from RFACTORY_STATE_KEY: a key file would differ between instances and they could not read each other's data")
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
        return self.backend.aggregate_count() > 0 or self._meta("schema") is not None

    def _meta(self, k: str):
        return self.backend.meta_get(k)

    def _init_meta(self) -> None:
        dek = Fernet.generate_key()
        self.backend.meta_put("schema", str(SCHEMA_VERSION).encode())
        self.backend.meta_put("wrapped_dek", self.provider.wrap(dek))
        self.backend.meta_put("key_check", Fernet(dek).encrypt(KEY_CHECK))
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

    def _read_objs(self, keys=None) -> dict[tuple[str, str], tuple[str, object]]:
        out = {}
        for kind, k, h, blob in self.backend.read(keys):
            try:
                plain = self._fernet.decrypt(blob)
            except InvalidToken:
                raise StoreError(f"aggregate {kind}/{k} failed authentication: the database was modified or corrupted")
            if hashlib.sha256(plain).hexdigest() != h:
                raise StoreError(f"aggregate {kind}/{k} does not match its recorded hash")
            out[(kind, k)] = (h, _loads(plain))
        return out

    def _adopt(self, loaded: dict[tuple[str, str], tuple[str, object]]) -> None:
        """Install loaded aggregates into the platform and remember both hashes: the database's and ours (re-serialised, so that an
        aggregate nobody changes is never rewritten just because pickling is not byte-stable across processes)."""
        apply(self.svc, {k: o for k, (_h, o) in loaded.items()})
        cur = collect(self.svc)
        for key, (h, _o) in loaded.items():
            self._remote[key] = h
            self._hashes[key] = hashlib.sha256(_dumps(cur[key])).hexdigest() if key in cur else h

    def load(self) -> int:
        """Restore the platform from the database. Refuses (never silently starts empty) on a wrong key, tampering, or a schema mismatch.
        With a shared database the whole start-up runs under the write lock, so two instances starting together cannot both create keys."""
        self.backend.acquire_write(120.0)
        try:
            with self._lock:
                ver = self._meta("schema")
                if ver is None:
                    if self.backend.aggregate_count():
                        raise StoreError("state database has data but no schema marker: refusing to load")
                    self._init_meta()
                    self._version = self.backend.version()
                    return 0
                self._open_existing()
                self._version = self.backend.version()
                loaded = self._read_objs()
                self._adopt(loaded)
                self.interrupted = self.find_interrupted()
                if self.interrupted:
                    self.svc.audit.append("system", "persistence.interrupted_work_found", "store", {"items": self.interrupted[:20]})
                return len(loaded)
        finally:
            self.backend.release_write()

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

    # ---------------------------------------------------------------- catching up with other instances
    def refresh(self) -> int:
        """Pick up what other instances saved since we last looked. One cheap query when nothing changed. Returns the aggregates replaced."""
        if self._fernet is None or not self.backend.multi_instance:
            return 0
        with self._lock:
            v = self.backend.version()
            if v == self._version:
                return 0
            try:
                self._fernet.decrypt(self._meta("key_check"))
            except InvalidToken:  # another instance rotated the data key: take the new one (needs the same key-encryption key)
                self._open_existing()
            remote = self.backend.list_hashes()
            changed = [k for k, h in remote.items() if self._remote.get(k) != h]
            gone = [k for k in self._hashes if k not in remote]
            loaded = self._read_objs(changed) if changed else {}
            self._adopt(loaded)
            for kind, k in gone:
                forget(self.svc, kind, k)
                self._hashes.pop((kind, k), None)
                self._remote.pop((kind, k), None)
            self._version = v
            self.refreshed += len(loaded) + len(gone)
            return len(loaded) + len(gone)

    def begin_write(self, timeout: float = 60.0) -> int:
        """Start of a mutating request: take the deployment's write lock, then catch up, so this request works on the latest state."""
        self.backend.acquire_write(timeout)
        try:
            self._write_held = True
            return self.refresh()
        except BaseException:
            self._write_held = False
            self.backend.release_write()
            raise

    def end_write(self) -> None:
        if self._write_held:
            self._write_held = False
            self.backend.release_write()

    # ---------------------------------------------------------------- save
    def save(self) -> dict:
        """Write every aggregate that changed since the last save, in one transaction."""
        with self._lock:
            if self._fernet is None:
                raise StoreError("the store has not been opened: call load() first")
            cur = collect(self.svc)
            changed: list[tuple[tuple[str, str], str, bytes]] = []
            for key, obj in cur.items():
                plain = _dumps(obj)
                h = hashlib.sha256(plain).hexdigest()
                if self._hashes.get(key) != h:
                    changed.append((key, h, plain))
            gone = [k for k in self._hashes if k not in cur]
            if changed or gone:
                v = self.backend.commit([(kind, k, h, self._fernet.encrypt(plain)) for (kind, k), h, plain in changed], gone)
                if self._write_held or not self.backend.multi_instance:
                    self._version = v  # nobody can have saved in between: we are current
            for (key, h, _p) in changed:
                self._hashes[key] = h
                self._remote[key] = h
            for k in gone:
                self._hashes.pop(k, None)
                self._remote.pop(k, None)
            self.last_saved = datetime.now(timezone.utc).isoformat()
            self.last_save_stats = {"written": len(changed), "deleted": len(gone), "aggregates": len(cur)}
            return self.last_save_stats

    # ---------------------------------------------------------------- key rotation
    @classmethod
    def open_for_maintenance(cls, path: Path | None, kek: bytes, backend: Backend | None = None) -> "StateStore":
        """Open a database without a running platform (offline key rotation)."""
        st = cls(None, path, key=kek, backend=backend)
        if st._meta("schema") is None:
            raise StoreError("no database to maintain")
        st._open_existing()
        return st

    def rotate_kek(self, new_provider: KeyProvider) -> None:
        """Re-wrap the data key under a new key-encryption key. Cheap: no blob is touched."""
        with self._lock:
            dek = self.provider.unwrap(self._meta("wrapped_dek"))
            self.backend.meta_put("wrapped_dek", new_provider.wrap(dek))
            self.provider = new_provider

    def rotate_dek(self) -> int:
        """Replace the data key and re-encrypt every stored blob in ONE transaction (all or nothing). Other instances notice at their next
        refresh (the key check no longer opens with their key) and take the new key."""
        owns = self._write_held  # inside a mutating request the lock is already ours
        if not owns:
            self.backend.acquire_write(60.0)
        try:
            with self._lock:
                if self.svc is not None:  # a maintenance tool has no running platform to catch up
                    self.refresh()
                old, new_key = self._fernet, Fernet.generate_key()
                new = Fernet(new_key)
                n = self.backend.rewrite(lambda blob: new.encrypt(old.decrypt(blob)),
                                         {"wrapped_dek": self.provider.wrap(new_key), "key_check": new.encrypt(KEY_CHECK)})
                self._fernet = new
                self._version = self.backend.version()
                return n
        finally:
            if not owns:
                self.backend.release_write()

    def status(self) -> dict:
        kinds: dict[str, int] = {}
        for kind, _ in self._hashes:
            kinds[kind] = kinds.get(kind, 0) + 1
        multi = self.backend.multi_instance
        d = self.backend.describe()
        limits = ["single writer: mutating API requests are serialised" + (" across all instances (one write lock in the database)" if multi else ""),
                  "no schema migrations", "no online backup or version history",
                  "the audit log is " + ("an append-only table in the same database (not encrypted)" if multi else "audit.jsonl, append-only on disk and not encrypted")]
        if not multi:
            limits.insert(1, "SQLite: one process only; use RFACTORY_DATABASE_URL (PostgreSQL) to run several instances")
        else:
            limits.append("state is replicated in each instance's memory and caught up at every request (one query when nothing changed)")
        return {"durable": True, **d, "bytes": self.backend.size(), "aggregates": len(self._hashes), "by_kind": kinds, "schema_version": SCHEMA_VERSION,
                "interrupted_work": self.interrupted, "encrypted_at_rest": True, "key_source": self.key_source, "key_provider": self.provider.name,
                "envelope_encryption": True, "last_saved": self.last_saved, "last_save": self.last_save_stats, "version": self._version,
                "aggregates_taken_from_other_instances": self.refreshed, "limits": limits}
