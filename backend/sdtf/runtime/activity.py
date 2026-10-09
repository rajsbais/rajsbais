"""Simulated business activity on a synthetic source after the initial extraction.

A real source keeps changing while the initial load runs; the delta engine replays those changes. Without an SAP
system this module plays the business: it creates and changes documents and masters in the source record store
and writes the change log that the simulated add-on serves through Z_SDTF_CDC_POLL. Changes are deterministic
per seed, spread across company codes (in and out of scope) and respect a declared business freeze.
"""
from __future__ import annotations

import random
import time
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..catalog.store import RecordStore, delete_records, upsert_records
from ..catalog.tables import record_key
from ..models import SapSystem, SourceChangeEvent

KINDS = ("NEW_SALES_ORDER", "UPDATE_SALES_ORDER_ITEM_QTY", "NEW_FI_DOCUMENT", "UPDATE_CUSTOMER", "DELETE_SALES_ORDER_ITEM")


def _ts() -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime())


class _Changes:
    def __init__(self, session: Session, system: SapSystem, actor: str):
        self.session, self.system, self.actor = session, system, actor
        self.seq = int(session.execute(select(func.max(SourceChangeEvent.seq)).where(SourceChangeEvent.system_id == system.id)).scalar() or 0)
        self.changenr = 0
        self.events: list[SourceChangeEvent] = []

    def change_set(self) -> str:
        self.changenr += 1
        return f"CHG{self.seq:06d}{self.changenr:03d}"

    def write(self, changenr: str, object_type: str, table: str, row: dict, op: str) -> None:
        key = record_key(table, row)
        if op == "D":
            delete_records(self.session, self.system.id, table, [key])
        else:
            upsert_records(self.session, self.system.id, table, [row])
        self.seq += 1
        self.events.append(SourceChangeEvent(system_id=self.system.id, seq=self.seq, changenr=changenr, object_type=object_type, table_name=table, record_key=key, op=op, changed_at=_ts(), changed_by=self.actor, payload=None if op == "D" else dict(row)))


def _lines_by_doc(store: RecordStore) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for l in store.rows("BSEG"):
        out.setdefault(f"{l['BUKRS']}|{l['BELNR']}|{l['GJAHR']}", []).append(l)
    return out


def simulate_business_activity(session: Session, system: SapSystem, seed: int = 1, count: int = 10, company_codes: list[str] | None = None, frozen_ccs: set[str] | None = None, actor: str = "BUSINESS_SIM") -> dict:
    """Apply `count` business changes to the source. Each change is one change set (document + items). Returns a summary."""
    rng = random.Random(seed)
    store = RecordStore.load(session, system.id)
    frozen = set(frozen_ccs or ())
    ccs = company_codes or sorted({r["BUKRS"] for r in store.rows("T001")})
    if not ccs:
        return {"applied": 0, "events": 0, "by_kind": {}, "by_company_code": {}, "blocked_by_freeze": 0, "watermark": "0"}
    ch = _Changes(session, system, actor)
    by_kind: Counter = Counter()
    by_cc: Counter = Counter()
    blocked = 0
    vkorg_of_cc = {r["VKORG"]: r["BUKRS"] for r in store.rows("TVKO")}
    cc_of_plant = {r["BWKEY"]: r["BUKRS"] for r in store.rows("T001K")}
    vbak_by_cc: dict[str, list[dict]] = {}
    for r in store.rows("VBAK"):
        vbak_by_cc.setdefault(vkorg_of_cc.get(r.get("VKORG"), r.get("BUKRS_VF")), []).append(r)

    def own_plants(order: dict, cc: str) -> bool:  # new business is created in the company code's own plants (cross-company orders are not cloned)
        items = store.lookup("VBAP", "VBELN", order["VBELN"])
        return bool(items) and all(cc_of_plant.get(i.get("WERKS")) == cc for i in items)
    bkpf_by_cc: dict[str, list[dict]] = {}
    gl_only = {h for h, lines in _lines_by_doc(store).items() if all(l.get("KOART") == "S" for l in lines)}
    for r in store.rows("BKPF"):
        if not r.get("BSTAT") and not r.get("BVORG") and f"{r['BUKRS']}|{r['BELNR']}|{r['GJAHR']}" in gl_only:
            bkpf_by_cc.setdefault(r["BUKRS"], []).append(r)  # G/L-only templates: open items follow from customer/supplier lines in the target
    next_vbeln = max((int(r["VBELN"]) for r in store.rows("VBAK") if str(r["VBELN"]).isdigit()), default=1000000) + 1
    next_belnr = max((int(r["BELNR"]) for r in store.rows("BKPF") if str(r["BELNR"]).isdigit()), default=100000000) + 1
    created_orders: list[dict] = []
    for i in range(count):
        kind = KINDS[i % len(KINDS)]
        cc = rng.choice(ccs)
        if cc in frozen:
            blocked += 1
            continue
        cs = ch.change_set()
        if kind == "NEW_SALES_ORDER":
            clean = [r for r in vbak_by_cc.get(cc, []) if own_plants(r, cc)]
            tmpl = rng.choice(clean) if clean else None
            if tmpl is None:
                continue
            items = store.lookup("VBAP", "VBELN", tmpl["VBELN"])
            hdr = {**tmpl, "VBELN": str(next_vbeln), "AUDAT": time.strftime("%Y%m%d", time.gmtime()), "GBSTK": "A", "NETWR": round(sum(float(x["NETWR"]) for x in items), 2)}
            next_vbeln += 1
            ch.write(cs, "SD.SalesOrder", "VBAK", hdr, "I")
            new_items = []
            for it in items:
                row = {**it, "VBELN": hdr["VBELN"]}
                ch.write(cs, "SD.SalesOrder", "VBAP", row, "I")
                new_items.append(row)
            created_orders.append(hdr)
            vbak_by_cc.setdefault(cc, []).append(hdr)
        elif kind == "UPDATE_SALES_ORDER_ITEM_QTY":
            # the customer changes an item quantity: the item is re-priced at its unit price, the header total follows
            cands = [r for r in vbak_by_cc.get(cc, []) if r.get("GBSTK") != "C" and store.lookup("VBAP", "VBELN", r["VBELN"])]
            if not cands:
                continue
            hdr = rng.choice(cands)
            items = sorted(store.lookup("VBAP", "VBELN", hdr["VBELN"]), key=lambda x: int(x["POSNR"]))
            it = items[0]
            old_qty = float(it["KWMENG"]) or 1.0
            new_qty = max(1, int(old_qty) + rng.choice((-2, -1, 1, 2, 3)))
            new_item = {**it, "KWMENG": new_qty, "NETWR": round(float(it["NETWR"]) / old_qty * new_qty, 2)}
            ch.write(cs, "SD.SalesOrder", "VBAP", new_item, "U")
            total = round(sum(float(x["NETWR"]) for x in items[1:]) + new_item["NETWR"], 2)
            new_hdr = {**hdr, "NETWR": total}
            ch.write(cs, "SD.SalesOrder", "VBAK", new_hdr, "U")
            vbak_by_cc[cc] = [new_hdr if r["VBELN"] == hdr["VBELN"] else r for r in vbak_by_cc[cc]]
            store._tables["VBAP"] = [new_item if (x["VBELN"] == it["VBELN"] and x["POSNR"] == it["POSNR"]) else x for x in store._tables["VBAP"]]
            store._indexes.pop(("VBAP", "VBELN"), None)
        elif kind == "NEW_FI_DOCUMENT":
            tmpl = rng.choice(bkpf_by_cc[cc]) if bkpf_by_cc.get(cc) else None
            if tmpl is None:
                continue
            lines = store.lookup("BSEG", "BELNR", tmpl["BELNR"])
            lines = [x for x in lines if x["BUKRS"] == tmpl["BUKRS"] and str(x["GJAHR"]) == str(tmpl["GJAHR"])]
            today = time.strftime("%Y%m%d", time.gmtime())
            hdr = {**tmpl, "BELNR": str(next_belnr).zfill(len(str(tmpl["BELNR"]))), "GJAHR": int(today[:4]), "BLDAT": today, "BUDAT": today, "MONAT": int(today[4:6]), "AWKEY": "", "XBLNR": f"SIM-{cs}"}
            next_belnr += 1
            ch.write(cs, "FI.AccountingDocument", "BKPF", hdr, "I")
            for ln in lines:
                ch.write(cs, "FI.AccountingDocument", "BSEG", {**ln, "BELNR": hdr["BELNR"], "GJAHR": hdr["GJAHR"], "AUGBL": "", "AUGDT": ""}, "I")
        elif kind == "UPDATE_CUSTOMER":
            views = [r for r in store.rows("KNB1") if r["BUKRS"] == cc]
            if not views:
                continue
            cust = store.by_key("KNA1", rng.choice(views)["KUNNR"])
            if cust is None:
                continue
            base_name = str(cust["NAME1"]).split(" (renamed")[0]
            row = {**cust, "NAME1": f"{base_name} (renamed {ch.seq + 1})"}
            ch.write(cs, "MD.Customer", "KNA1", row, "U")
        elif kind == "DELETE_SALES_ORDER_ITEM":
            cands = [r for r in vbak_by_cc.get(cc, []) if r.get("GBSTK") != "C" and len(store.lookup("VBAP", "VBELN", r["VBELN"])) > 1]
            cands = [r for r in cands if not any(o["VBELN"] == r["VBELN"] for o in created_orders)]
            if not cands:
                continue
            hdr = rng.choice(cands)
            items = sorted(store.lookup("VBAP", "VBELN", hdr["VBELN"]), key=lambda x: int(x["POSNR"]))
            last = items[-1]
            ch.write(cs, "SD.SalesOrder", "VBAP", last, "D")
            new_hdr = {**hdr, "NETWR": round(float(hdr["NETWR"]) - float(last["NETWR"]), 2)}
            ch.write(cs, "SD.SalesOrder", "VBAK", new_hdr, "U")
            store._tables["VBAP"] = [x for x in store._tables["VBAP"] if not (x["VBELN"] == last["VBELN"] and x["POSNR"] == last["POSNR"])]
            store._indexes.pop(("VBAP", "VBELN"), None)
        by_kind[kind] += 1
        by_cc[cc] += 1
    session.add_all(ch.events)
    session.flush()
    return {"applied": sum(by_kind.values()), "events": len(ch.events), "by_kind": dict(by_kind), "by_company_code": dict(by_cc), "blocked_by_freeze": blocked, "watermark": str(ch.seq)}


def load_change_log(session: Session, system_id: str) -> list[dict]:
    """The source's change log in the add-on's event shape (what Z_SDTF_CDC_POLL serves)."""
    from .rfc import row_json

    rows = session.execute(select(SourceChangeEvent).where(SourceChangeEvent.system_id == system_id).order_by(SourceChangeEvent.seq)).scalars().all()
    return [{"SEQ": e.seq, "CHANGENR": e.changenr, "OBJECT_TYPE": e.object_type, "TABNAME": e.table_name, "KEY": e.record_key, "OP": e.op, "CHANGED_AT": e.changed_at, "CHANGED_BY": e.changed_by, "JSON": row_json(e.payload) if e.payload is not None else ""} for e in rows]
