"""Storage backends for the platform state.

  SqliteBackend      one file, one process. The default for development and a single instance.
  PostgresBackend    a shared PostgreSQL database for SEVERAL platform instances behind a load balancer.

What the PostgreSQL backend gives, and how:
  * the same encrypted aggregate blobs as SQLite (the database never sees plaintext, keys, or the data key);
  * one cross-instance WRITE LOCK (a session-level advisory lock held on a dedicated connection): a mutating request takes it, first catches up
    with what other instances saved, does its work, saves, and releases it. Two instances can therefore never overwrite each other's changes;
    if an instance dies mid-request its connection drops and PostgreSQL releases the lock by itself;
  * a version counter bumped in the same transaction as every save, so a reading instance can tell with one cheap query whether it is behind;
  * the audit log in an append-only table (a trigger refuses UPDATE, DELETE and TRUNCATE) whose chain is extended under its own lock, so entries
    written by different instances still form ONE valid hash chain.

What it does not give: horizontal write scalability (mutating requests are serialised on purpose), automatic failover or backups (that is the
database operator's), row-level security or per-tenant schemas, and a trigger is not a defence against the database owner (the signed hash chain
and an escrowed head are: see /api/audit/head).
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path


class BackendError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Backend:
    name = "abstract"
    multi_instance = False

    def meta_get(self, key: str) -> bytes | None: raise NotImplementedError
    def meta_put(self, key: str, value: bytes) -> None: raise NotImplementedError
    def aggregate_count(self) -> int: raise NotImplementedError
    def list_hashes(self) -> dict[tuple[str, str], str]: raise NotImplementedError
    def read(self, keys: list[tuple[str, str]] | None = None) -> Iterator[tuple[str, str, str, bytes]]: raise NotImplementedError
    def commit(self, changes: list[tuple[str, str, str, bytes]], deletes: list[tuple[str, str]]) -> int: raise NotImplementedError
    def version(self) -> int: raise NotImplementedError
    def rewrite(self, transform: Callable[[bytes], bytes], meta: dict[str, bytes]) -> int: raise NotImplementedError
    def size(self) -> int: raise NotImplementedError
    def describe(self) -> dict: raise NotImplementedError
    def acquire_write(self, timeout: float) -> None: pass
    def release_write(self) -> None: pass
    def close(self) -> None: pass


# ---------------------------------------------------------------------------------------------------------------- SQLite
class SqliteBackend(Backend):
    name = "sqlite"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS aggregates (kind TEXT NOT NULL, id TEXT NOT NULL, hash TEXT NOT NULL, blob BLOB NOT NULL, updated TEXT NOT NULL, PRIMARY KEY (kind, id))")
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB NOT NULL)")

    def meta_get(self, key):
        r = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return bytes(r[0]) if r else None

    def meta_put(self, key, value):
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def aggregate_count(self):
        return self._db.execute("SELECT COUNT(*) FROM aggregates").fetchone()[0]

    def list_hashes(self):
        return {(k, i): h for k, i, h in self._db.execute("SELECT kind, id, hash FROM aggregates")}

    def read(self, keys=None):
        if keys is None:
            yield from ((k, i, h, bytes(b)) for k, i, h, b in self._db.execute("SELECT kind, id, hash, blob FROM aggregates"))
        else:
            for k, i in keys:
                r = self._db.execute("SELECT kind, id, hash, blob FROM aggregates WHERE kind=? AND id=?", (k, i)).fetchone()
                if r:
                    yield r[0], r[1], r[2], bytes(r[3])

    def commit(self, changes, deletes):
        now = _now()
        with self._db:
            for kind, k, h, blob in changes:
                self._db.execute("INSERT OR REPLACE INTO aggregates VALUES (?,?,?,?,?)", (kind, k, h, blob, now))
            for kind, k in deletes:
                self._db.execute("DELETE FROM aggregates WHERE kind=? AND id=?", (kind, k))
            v = self.version() + 1
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)", (str(v).encode(),))
        return v

    def version(self):
        r = self.meta_get("version")
        return int(r) if r else 0

    def rewrite(self, transform, meta):
        rows = self._db.execute("SELECT kind, id, blob FROM aggregates").fetchall()
        with self._db:
            for kind, k, blob in rows:
                self._db.execute("UPDATE aggregates SET blob=? WHERE kind=? AND id=?", (transform(bytes(blob)), kind, k))
            for mk, mv in meta.items():
                self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (mk, mv))
        return len(rows)

    def size(self):
        return self.path.stat().st_size if self.path.exists() else 0

    def describe(self):
        return {"backend": "sqlite", "database": self.path.name, "multi_instance": False}

    def close(self):
        self._db.close()


# ---------------------------------------------------------------------------------------------------------------- PostgreSQL
WRITE_LOCK_KEY = 7_268_411_001
AUDIT_LOCK_KEY = 7_268_411_002
SCHEMA_LOCK_KEY = 7_268_411_003

DDL = [
    "CREATE TABLE IF NOT EXISTS rf_aggregates (kind text NOT NULL, id text NOT NULL, hash text NOT NULL, blob bytea NOT NULL, updated timestamptz NOT NULL, PRIMARY KEY (kind, id))",
    "CREATE TABLE IF NOT EXISTS rf_meta (key text PRIMARY KEY, value bytea NOT NULL)",
    "CREATE TABLE IF NOT EXISTS rf_version (id int PRIMARY KEY CHECK (id = 1), n bigint NOT NULL)",
    "INSERT INTO rf_version (id, n) VALUES (1, 0) ON CONFLICT DO NOTHING",
    "CREATE TABLE IF NOT EXISTS rf_audit (seq bigint PRIMARY KEY, entry text NOT NULL)",
    """CREATE OR REPLACE FUNCTION rf_audit_immutable() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'rf_audit is append-only'; END $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS rf_audit_no_change ON rf_audit",
    "CREATE TRIGGER rf_audit_no_change BEFORE UPDATE OR DELETE ON rf_audit FOR EACH ROW EXECUTE FUNCTION rf_audit_immutable()",
    "DROP TRIGGER IF EXISTS rf_audit_no_truncate ON rf_audit",
    "CREATE TRIGGER rf_audit_no_truncate BEFORE TRUNCATE ON rf_audit FOR EACH STATEMENT EXECUTE FUNCTION rf_audit_immutable()",
]


class PostgresBackend(Backend):
    name = "postgresql"
    multi_instance = True

    def __init__(self, url: str):
        try:
            import psycopg
        except ImportError as e:  # pragma: no cover - exercised only where the driver is absent
            raise BackendError("PostgreSQL state needs the psycopg driver: pip install 'psycopg[binary]'") from e
        self._psycopg = psycopg
        self.url = url
        self._io = threading.RLock()  # one data connection, used by one thread at a time
        self._wl = threading.Lock()  # one write-lock holder per process
        self._locked = False
        self._conn = self._connect(autocommit=False)
        self._lock_conn = self._connect(autocommit=True)
        self._init_schema()

    # ---- connections
    def _connect(self, autocommit: bool):
        try:
            return self._psycopg.connect(self.url, autocommit=autocommit, connect_timeout=10)
        except self._psycopg.Error as e:
            raise BackendError(f"cannot connect to the PostgreSQL database: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}") from e

    def _data(self):
        if self._conn.closed:
            self._conn = self._connect(autocommit=False)
        return self._conn

    def _tx(self, fn):
        """Run fn(cursor) in one transaction on the data connection; roll back on any error. A dropped connection is re-made once for reads."""
        with self._io:
            try:
                with self._data().cursor() as cur:
                    out = fn(cur)
                self._conn.commit()
                return out
            except self._psycopg.OperationalError as e:
                self._conn.rollback() if not self._conn.closed else None
                raise BackendError(f"the PostgreSQL connection failed: {type(e).__name__}: {e}") from e
            except BaseException:
                if not self._conn.closed:
                    self._conn.rollback()
                raise

    def _init_schema(self) -> None:
        def go(cur):
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))  # instances starting together do not race on the DDL
            for stmt in DDL:
                cur.execute(stmt)
        self._tx(go)

    # ---- meta and aggregates
    def meta_get(self, key):
        def go(cur):
            cur.execute("SELECT value FROM rf_meta WHERE key=%s", (key,))
            r = cur.fetchone()
            return bytes(r[0]) if r else None
        return self._tx(go)

    def meta_put(self, key, value):
        self._tx(lambda cur: cur.execute("INSERT INTO rf_meta VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value)))

    def aggregate_count(self):
        def go(cur):
            cur.execute("SELECT COUNT(*) FROM rf_aggregates")
            return cur.fetchone()[0]
        return self._tx(go)

    def list_hashes(self):
        def go(cur):
            cur.execute("SELECT kind, id, hash FROM rf_aggregates")
            return {(k, i): h for k, i, h in cur.fetchall()}
        return self._tx(go)

    def read(self, keys=None):
        def go(cur):
            if keys is None:
                cur.execute("SELECT kind, id, hash, blob FROM rf_aggregates")
                return [(k, i, h, bytes(b)) for k, i, h, b in cur.fetchall()]
            out = []
            for k, i in keys:
                cur.execute("SELECT kind, id, hash, blob FROM rf_aggregates WHERE kind=%s AND id=%s", (k, i))
                r = cur.fetchone()
                if r:
                    out.append((r[0], r[1], r[2], bytes(r[3])))
            return out
        yield from self._tx(go)

    def commit(self, changes, deletes):
        def go(cur):
            for kind, k, h, blob in changes:
                cur.execute("INSERT INTO rf_aggregates (kind, id, hash, blob, updated) VALUES (%s,%s,%s,%s, now()) "
                            "ON CONFLICT (kind, id) DO UPDATE SET hash = EXCLUDED.hash, blob = EXCLUDED.blob, updated = EXCLUDED.updated", (kind, k, h, blob))
            for kind, k in deletes:
                cur.execute("DELETE FROM rf_aggregates WHERE kind=%s AND id=%s", (kind, k))
            cur.execute("UPDATE rf_version SET n = n + 1 WHERE id = 1 RETURNING n")  # in the same transaction as the data it announces
            return cur.fetchone()[0]
        return self._tx(go)

    def version(self):
        def go(cur):
            cur.execute("SELECT n FROM rf_version WHERE id = 1")
            return cur.fetchone()[0]
        return self._tx(go)

    def rewrite(self, transform, meta):
        def go(cur):
            cur.execute("SELECT kind, id, blob FROM rf_aggregates FOR UPDATE")
            rows = cur.fetchall()
            for kind, k, blob in rows:
                cur.execute("UPDATE rf_aggregates SET blob=%s WHERE kind=%s AND id=%s", (transform(bytes(blob)), kind, k))
            for mk, mv in meta.items():
                cur.execute("INSERT INTO rf_meta VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (mk, mv))
            cur.execute("UPDATE rf_version SET n = n + 1 WHERE id = 1")
            return len(rows)
        return self._tx(go)

    def size(self):
        def go(cur):
            cur.execute("SELECT COALESCE(sum(octet_length(blob)), 0) FROM rf_aggregates")
            return int(cur.fetchone()[0])
        return self._tx(go)

    def describe(self):
        def go(cur):
            cur.execute("SELECT current_database(), current_setting('server_version')")
            return cur.fetchone()
        db, ver = self._tx(go)
        return {"backend": "postgresql", "database": db, "server_version": ver, "multi_instance": True}

    # ---- the cross-instance write lock
    def acquire_write(self, timeout: float = 60.0) -> None:
        """Take the write lock of the whole deployment. Polls so that a stuck holder produces an error, not a hang."""
        if not self._wl.acquire(timeout=timeout):
            raise BackendError(f"another request of this instance held the write lock for more than {timeout:.0f}s")
        deadline = time.monotonic() + timeout
        try:
            while True:
                with self._lock_conn.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s)", (WRITE_LOCK_KEY,))
                    if cur.fetchone()[0]:
                        self._locked = True
                        return
                if time.monotonic() >= deadline:
                    raise BackendError(f"another instance held the write lock for more than {timeout:.0f}s")
                time.sleep(0.02)
        except BaseException:
            self._wl.release()
            raise

    def release_write(self) -> None:
        if not self._locked:
            return
        try:
            with self._lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (WRITE_LOCK_KEY,))
        finally:
            self._locked = False
            self._wl.release()

    # ---- the audit log
    def audit_tail(self, after_seq: int) -> list[str]:
        def go(cur):
            cur.execute("SELECT entry FROM rf_audit WHERE seq > %s ORDER BY seq", (after_seq,))
            return [r[0] for r in cur.fetchall()]
        return self._tx(go)

    def audit_append(self, build: Callable[[int, str | None], tuple[dict, str]]) -> dict:
        """Extend the chain atomically: under the audit lock, read the current end, let `build` make the next entry from it, insert it."""
        import json

        def go(cur):
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK_KEY,))
            cur.execute("SELECT seq, entry FROM rf_audit ORDER BY seq DESC LIMIT 1")
            r = cur.fetchone()
            prev_seq, prev_hash = (r[0], json.loads(r[1])["hash"]) if r else (0, None)
            entry, text = build(prev_seq, prev_hash)
            cur.execute("INSERT INTO rf_audit (seq, entry) VALUES (%s, %s)", (entry["seq"], text))
            return entry
        return self._tx(go)

    def close(self):
        for c in (self._conn, self._lock_conn):
            with contextlib.suppress(Exception):
                c.close()
