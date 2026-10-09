"""A fake of the ZRF_* loader function group (docs/abap/ZRF_LOADER.abap), serving a SimulatedSap target.

It implements the CONTRACT the platform relies on, as written in the ABAP spec: a handshake, an allow list, an enable switch, a client
category, a row cap, authorization per table, dry runs, all-or-nothing batches and a call log. It is a model of code that has never run on a
real system: it proves the platform side behaves correctly against the contract, not that the ABAP behaves.
"""
from __future__ import annotations

import json

from ..ddic import TABLES
from .fake_rfc import FakeRfcTransport
from .rfc import RemoteAuthError, RemoteError

MAX_ROWS = 500  # the SAP-side cap (the platform sends at most 200)


class FakeLoaderTransport(FakeRfcTransport):
    def __init__(self, sap, *, category: str = "T", enabled: bool = True, allowed: set[str] | None = None, version: str = "RFL-1",
                 unauthorised: set[str] | None = None, sid: str | None = None, client: str | None = None, **kw):
        super().__init__(sap, **kw)
        self.category, self.enabled, self.version = category, enabled, version
        self.allowed = set(allowed) if allowed is not None else {t for t, d in TABLES.items() if not d.config} | {"NRIV"}
        self.unauthorised = unauthorised or set()
        self.sid_override, self.client_override = sid, client
        self.log: list[dict] = []  # ZRF_LOG
        self.fail_after: int | None = None  # simulate an error in the middle of a batch (the SAP side must roll it back)

    def call(self, function: str, **p) -> dict:
        if not function.startswith("ZRF_"):
            return super().call(function, **p)
        self.calls.append({"function": function, **{k: (v if k != "IV_ROWS" and k != "IV_KEYS" else f"<{len(v)} chars>") for k, v in p.items()}, "fields": []})
        if self._fail:
            raise self._fail.pop(0)
        if function == "ZRF_PING":
            s = self.sap.system
            return {"EV_VERSION": self.version, "EV_SYSID": self.sid_override or s.sid, "EV_CLIENT": self.client_override or s.client, "EV_CLIENT_CATEGORY": self.category,
                    "EV_WRITES_ENABLED": "X" if self.enabled else "", "ET_ALLOWED": sorted(self.allowed)}
        if function in ("ZRF_UPSERT", "ZRF_DELETE"):
            return self._write(function, p)
        if function == "ZRF_NR_SET":
            return self._nr(p)
        raise RemoteError(f"function {function} is not available in the fake")

    def _guard(self, table: str, dry: bool, rid: str, op: str, n: int) -> None:
        if self.category == "P":
            raise RemoteError("LOADER: productive client")
        if not self.enabled:
            raise RemoteError("LOADER: writes not enabled (ZRF_CFG)")
        if table not in self.allowed:
            raise RemoteError(f"LOADER: table {table} is not on the allow list (ZRF_ALLOW)")
        if table in self.unauthorised:
            raise RemoteAuthError(f"NOT_AUTHORIZED: S_TABU_NAM {table} ACTVT 02")
        if n > MAX_ROWS:
            raise RemoteError(f"LOADER: more than {MAX_ROWS} rows in one call")

    def _write(self, function: str, p: dict) -> dict:
        table, dry, rid = p["IV_TABLE"], p.get("IV_DRY_RUN") == "X", p.get("IV_REQUEST_ID", "")
        items = json.loads(p.get("IV_ROWS") or p.get("IV_KEYS") or "[]")
        op = "upsert" if function == "ZRF_UPSERT" else "delete"
        self._guard(table, dry, rid, op, len(items))
        kf = TABLES[table].keys
        for it in items:  # validate every row before touching anything
            if any(k not in it for k in kf):
                self._log(rid, table, op, 0, dry, "missing key field")
                return {"EV_WRITTEN": 0, "ET_MESSAGES": [{"MESSAGE": "a row lacks a key field"}]}
            if function == "ZRF_UPSERT" and any(f not in it for f in TABLES[table].fields):
                self._log(rid, table, op, 0, dry, "missing field")
                return {"EV_WRITTEN": 0, "ET_MESSAGES": [{"MESSAGE": "a row lacks a field of the structure"}]}
        if dry:
            self._log(rid, table, op, len(items), True, "")
            return {"EV_WRITTEN": len(items), "ET_MESSAGES": []}
        snapshot = [dict(r) for r in self.sap.data[table]]
        try:
            for n, it in enumerate(items):
                if self.fail_after is not None and n == self.fail_after:
                    raise RemoteError("simulated database error")
                if function == "ZRF_UPSERT":
                    self.sap.upsert(table, [it])
                else:
                    self.sap.delete(table, tuple(it[k] for k in kf))
        except Exception as e:  # noqa: BLE001 - all or nothing: the SAP side rolls the batch back
            self.sap.data[table] = snapshot
            self.sap._invalidate(table)
            self._log(rid, table, op, 0, False, f"rolled back: {e}")
            return {"EV_WRITTEN": 0, "ET_MESSAGES": [{"MESSAGE": f"rolled back: {e}"}]}
        self._log(rid, table, op, len(items), False, "")
        return {"EV_WRITTEN": len(items), "ET_MESSAGES": []}

    def _nr(self, p: dict) -> dict:
        self._guard("NRIV", False, p.get("IV_REQUEST_ID", ""), "level", 1)
        self.sap.set_number_level(p["IV_OBJECT"], int(p["IV_LEVEL"]))
        self._log(p.get("IV_REQUEST_ID", ""), "NRIV", "level", 1, False, "")
        return {"EV_WRITTEN": 1, "ET_MESSAGES": []}

    def _log(self, rid: str, table: str, op: str, n: int, dry: bool, msg: str) -> None:
        self.log.append({"request": rid, "table": table, "op": op, "rows": n, "dry": dry, "message": msg})
