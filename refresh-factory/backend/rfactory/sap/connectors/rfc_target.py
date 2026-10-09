"""Write side for a NON-PRODUCTION SAP system over RFC, through a small custom function group (ZRF_*) that the system's owner installs.

IMPORTANT STATUS: the ABAP side (docs/abap/ZRF_LOADER.abap) has NEVER been compiled or run, and this adapter has only run against a fake that
implements the same contract (fake_loader.py). Install it in a sandbox first. Standard RFC cannot write arbitrary tables, so there is no way
around custom code: the safeguards below are therefore split between the ABAP side (which the SAP owner controls and can read) and this side.

Safeguards, all of which must hold for any write:
  * the platform only arms this adapter for a system that is NOT production (platform role) and was approved for writing by a second person;
  * the remote handshake (ZRF_PING) must report: the loader's protocol version, the expected SID and client, a client category that is NOT
    production, and writes enabled by the SAP-side administrator (table ZRF_CFG);
  * writes are limited to tables the SAP-side administrator put on the allow list (ZRF_ALLOW) AND that the platform's DDIC model knows;
  * every write call is checked against the SAP authorization S_TABU_NAM (activity 02) of the connected user, capped at 200 rows, logged on
    the SAP side (ZRF_LOG) with the request id, and preceded by a dry run of the same rows;
  * `assert_writable()` (called by the executor before every run) arms the adapter for a short time; an unarmed adapter refuses every write;
  * the function allow-list of the transport grows by exactly the ZRF_* functions here and never includes anything else.
"""
from __future__ import annotations

import json
import time
import uuid

from ..adapter import ProductionWriteBlocked, Row, SapSystem
from ..ddic import TABLES
from .profile import ConnectionProfile
from .rfc import ALLOWED_FUNCTIONS, AllowListTransport, RemoteError, RfcSourceAdapter, RfcTransport

PROTOCOL = "RFL-1"
WRITE_FUNCTIONS = frozenset({"ZRF_PING", "ZRF_UPSERT", "ZRF_DELETE", "ZRF_NR_SET"})
MAX_ROWS_PER_CALL = 200
ARM_SECONDS = 600


class LoaderError(RemoteError):
    pass


class RfcTargetAdapter(RfcSourceAdapter):
    """Read side of RfcSourceAdapter plus guarded writes. Only ever constructed for a system approved as a write target."""

    def __init__(self, system: SapSystem, transport: RfcTransport, profile: ConnectionProfile | None = None, *, clock=time.monotonic, **kw):
        super().__init__(system, transport, profile, clock=clock, **kw)
        self._t = AllowListTransport(transport, ALLOWED_FUNCTIONS | WRITE_FUNCTIONS)
        self._armed_until = 0.0
        self.writes: list[dict] = []  # what this adapter wrote (request id, table, operation, rows): kept for evidence
        self.handshake_info: dict = {}

    # ------------------------------------------------------------ handshake and arming
    def handshake(self) -> dict:
        r = self._call("ZRF_PING")
        info = {"version": r.get("EV_VERSION"), "sid": r.get("EV_SYSID"), "client": r.get("EV_CLIENT"), "category": r.get("EV_CLIENT_CATEGORY"),
                "writes_enabled": r.get("EV_WRITES_ENABLED") == "X", "allowed_tables": sorted({_name(t) for t in r.get("ET_ALLOWED", []) if _name(t)})}
        self.handshake_info = info
        if info["version"] != PROTOCOL:
            raise LoaderError(f"the installed loader speaks {info['version']!r}, this platform needs {PROTOCOL!r}")
        if str(info["sid"]).strip().upper() != self.system.sid.upper():
            raise LoaderError(f"the loader reports system {info['sid']!r} but this connection is registered as {self.system.sid!r}: wrong system")
        if self.system.client and str(info["client"]).strip() != self.system.client:
            raise LoaderError(f"the loader reports client {info['client']!r} but this connection is registered for client {self.system.client!r}")
        if str(info["category"]).strip().upper() == "P":
            raise ProductionWriteBlocked(f"{self.system.sid}/{info['client']} is a PRODUCTIVE client (client role P): the platform never writes to it")
        if not info["writes_enabled"]:
            raise ProductionWriteBlocked("writes are not enabled on the SAP side (table ZRF_CFG): the system owner has not switched the loader on")
        return info

    def assert_writable(self) -> None:
        if self.system.is_production:
            raise ProductionWriteBlocked(f"{self.system.label} is a production system in the platform's landscape")
        if not self.system.writable_target_allowed:
            raise ProductionWriteBlocked(f"{self.system.label} is locked against being overwritten")
        self.handshake()
        self._armed_until = self._clock() + ARM_SECONDS

    def _require_armed(self) -> None:
        if self._clock() >= self._armed_until:
            raise ProductionWriteBlocked("the remote target is not armed: assert_writable() must succeed shortly before every write")
        if self.system.is_production or not self.system.writable_target_allowed:
            raise ProductionWriteBlocked(f"{self.system.label} is not an allowed write target")

    # ------------------------------------------------------------ writes
    def _table_ok(self, table: str) -> None:
        td = TABLES.get(table)
        if td is None or (td.config and table != "NRIV"):
            raise LoaderError(f"{table} is not a table the platform writes")
        allowed = self.handshake_info.get("allowed_tables", [])
        if table not in allowed:
            raise LoaderError(f"{table} is not on the SAP-side allow list (ZRF_ALLOW); ask the system owner to add it")

    def _send(self, function: str, table: str, payload_key: str, items: list, op: str) -> int:
        self._table_ok(table)
        written = 0
        for i in range(0, len(items), MAX_ROWS_PER_CALL):
            part = items[i:i + MAX_ROWS_PER_CALL]
            body = json.dumps(part, default=str, separators=(",", ":"))
            rid = uuid.uuid4().hex[:16]
            dry = self._call(function, IV_TABLE=table, **{payload_key: body}, IV_DRY_RUN="X", IV_REQUEST_ID=rid)
            if int(dry.get("EV_WRITTEN", 0)) != len(part):
                raise LoaderError(f"dry run of {op} on {table}: {dry.get('EV_WRITTEN')} of {len(part)} rows accepted ({_msgs(dry)})")
            res = self._call(function, IV_TABLE=table, **{payload_key: body}, IV_DRY_RUN="", IV_REQUEST_ID=rid)
            n = int(res.get("EV_WRITTEN", 0))
            if n != len(part):
                raise LoaderError(f"{op} on {table}: {n} of {len(part)} rows written ({_msgs(res)}); the SAP side rolled the batch back or part of it: check ZRF_LOG for request {rid}")
            self.writes.append({"request": rid, "table": table, "op": op, "rows": n})
            written += n
        return written

    def upsert(self, table: str, rows: list[Row]) -> None:
        self._require_armed()
        if rows:
            self._send("ZRF_UPSERT", table, "IV_ROWS", [dict(r) for r in rows], "upsert")
        self.invalidate(table)

    def delete(self, table: str, key: tuple) -> None:
        self._require_armed()
        self._send("ZRF_DELETE", table, "IV_KEYS", [dict(zip(TABLES[table].keys, key))], "delete")
        self.invalidate(table)

    def invalidate(self, table: str) -> None:
        """Nothing is cached here (every read is a fresh RFC call); present so callers can treat local and remote targets alike."""

    # ------------------------------------------------------------ number ranges, ownership, interfaces
    def number_level(self, obj: str) -> int | None:
        for r in self._fetch("NRIV", ["OBJECT", "=", "'" + obj.replace("'", "''") + "'"]):
            return int(r["NRLEVEL"])
        return None

    def set_number_level(self, obj: str, level: int) -> None:
        self._require_armed()
        self._table_ok("NRIV")
        r = self._call("ZRF_NR_SET", IV_OBJECT=obj, IV_LEVEL=str(int(level)), IV_DRY_RUN="", IV_REQUEST_ID=uuid.uuid4().hex[:16])
        if int(r.get("EV_WRITTEN", 0)) != 1:
            raise LoaderError(f"number range {obj} was not updated ({_msgs(r)})")
        self.writes.append({"request": "nr", "table": "NRIV", "op": f"level {obj}={level}", "rows": 1})

    def owner_of(self, table: str, key_s: str) -> str | None:
        return None  # a remote system has no platform reservations

    def outbound_interfaces(self) -> list[dict]:
        """The platform cannot see a remote system's outbound interfaces. Unless a security officer attested at approval that they are inactive,
        report one active unverified interface so the release gate holds the refresh instead of silently passing."""
        att = (self.profile.options.get("write") or {}).get("outbound_attested_by")
        if att:
            return []
        return [{"name": "UNVERIFIED: outbound interfaces of a remote system are not inspected (attest at approval)", "active": True}]

    def capabilities(self) -> dict:
        c = super().capabilities()
        return {**c, "writes": True, "write_via": "custom function group ZRF_* (never compiled by the platform authors)", "handshake": self.handshake_info or None}


def _name(x) -> str:
    """A table-of-names row as pyrfc returns it: a bare string or a one-field structure."""
    v = next(iter(x.values()), "") if isinstance(x, dict) else x
    return str(v).strip()


def _msgs(r: dict) -> str:
    m = r.get("ET_MESSAGES") or []
    return "; ".join(str(x.get("MESSAGE", x) if isinstance(x, dict) else x) for x in m)[:200] or "no message"
