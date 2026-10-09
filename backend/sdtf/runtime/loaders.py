"""Delta loaders: change events become calls on the released APIs of the target (ADR-0015).

For every business object the registry prescribes a load method; the loader turns a change set (the rows of one
business change, already transformed) into the API operations that method allows:
* API objects: deep insert of a new document, PATCH of updatable properties with If-Match, POST/PATCH/DELETE of
  items, DELETE / BLOCK / REVERSAL of headers as the binding permits; journal entries are posted as a whole and
  changed by reversal + re-posting, with the number the target assigned written back.
* CONFIG_TRANSPORT objects are matched against the configuration shell, never loaded.
* MIGRATION_COCKPIT-only objects, histories and direct-table objects are reported as not delta-loadable with the
  reason, so the cutover plan can schedule them (fresh extraction at the final delta, balance migration).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..catalog.api_bindings import ApiBinding, EntityBinding, binding_for
from ..catalog.business_objects import BUSINESS_OBJECTS, load_methods_for
from ..catalog.tables import TABLES, record_key
from .target_api import ApiError, TargetApiClient


@dataclass
class LoadResult:
    status: str  # APPLIED | SKIPPED_DUPLICATE | SKIPPED_MISSING | UNSUPPORTED | REJECTED_BY_TARGET | MATCHED | CONFIG_MISSING
    action: str = ""  # INSERTED | UPDATED | DELETED | REVERSED | REPOSTED | BLOCKED | DERIVED | MATCHED
    load_method: str = ""
    api_call: str = ""
    message: str = ""
    target_key: str | None = None  # the key the target holds the row under (may differ: internal numbering)
    target_row: dict | None = None  # the row as the target stores it (derived fields filled)
    extension_fields: int = 0


@dataclass
class EventView:
    """What the loader needs from a DeltaEvent without depending on the ORM."""

    seq: int
    table: str
    op: str
    record_key: str
    target_key: str | None
    target_payload: dict | None
    object_type: str
    object_key: str
    changenr: str
    result: LoadResult | None = None
    extra: dict = field(default_factory=dict)


class DeltaLoader:
    def __init__(self, client: TargetApiClient, target_product: str = "S4HANA", read_row=None):
        self.client = client
        self.product = target_product
        self._read_row = read_row  # optional direct row reader (simulated gateway) for parity checks

    # ------------------------------------------------------------------------------------- planning
    def method_for(self, object_type: str) -> tuple[str, str, str]:
        """(method, api name, explanation) from the S/4 compatibility registry."""
        bo = BUSINESS_OBJECTS.get(object_type)
        if bo is None:
            return "UNKNOWN", "", "table not in the business object catalog"
        methods = load_methods_for(bo, self.product)
        if not methods:
            return "UNSUPPORTED_FOR_TARGET", "", f"no load method registered for {object_type} on {self.product}"
        m = methods[0]
        return m.method, m.api, m.note

    # ------------------------------------------------------------------------------------- execution
    def load_change_set(self, events: list[EventView]) -> None:
        """Apply the events of one change set for one business object instance. Sets `result` on each event."""
        if not events:
            return
        object_type = events[0].object_type
        method, api, note = self.method_for(object_type)
        if method == "CONFIG_TRANSPORT":
            for e in events:
                exists = self._exists(e.table, e.target_key) if e.target_key else False
                e.result = LoadResult("MATCHED" if exists else "CONFIG_MISSING", "MATCHED" if exists else "", method, "", "configuration comes from the target shell; record matched, not loaded" if exists else "configuration object missing in the target shell: transport it, the delta cannot create it", e.target_key)
            return
        if method != "API":
            for e in events:
                e.result = LoadResult("UNSUPPORTED", "", method, "", f"{object_type} is loaded through {method.lower().replace('_', ' ')} ({api or note or 'see registry'}); changes after the snapshot are not re-posted per event and must be re-migrated at the final delta", e.target_key)
            return
        b = binding_for(object_type)
        if b is None:
            for e in events:
                e.result = LoadResult("UNSUPPORTED", "", method, api, f"{api or object_type} has no API binding yet; add one in catalog/api_bindings.py", e.target_key)
            return
        try:
            if b.protocol == "SOAP":
                self._load_journal(b, events)
            else:
                self._load_odata(b, events)
        except ApiError as ex:
            for e in events:
                if e.result is None:
                    e.result = LoadResult("REJECTED_BY_TARGET", "", "API", f"{b.service}", f"{ex.status} {ex.code}: {ex.message}", e.target_key)

    def _exists(self, table: str, key: str) -> bool:
        if self._read_row is not None:
            return self._read_row(table, key) is not None
        return False

    # -- OData objects
    def _load_odata(self, b: ApiBinding, events: list[EventView]) -> None:
        header_events = [e for e in events if e.table == b.header.table]
        item_events = [e for e in events if e.table != b.header.table]
        hdr_create = next((e for e in header_events if e.op == "I"), None)
        hdr_delete = next((e for e in header_events if e.op == "D"), None)
        if hdr_create is not None:
            self._create_document(b, hdr_create, item_events if b.deep_insert else [])
            if not b.deep_insert:
                for e in item_events:
                    self._item_op(b, e)
            return
        if hdr_delete is not None:
            self._delete_document(b, hdr_delete, item_events)
            return
        for e in item_events:
            self._item_op(b, e)
        for e in header_events:  # updates
            self._update_entity(b, b.header, e)

    def _create_document(self, b: ApiBinding, hdr: EventView, items: list[EventView]) -> None:
        eb = b.header
        payload = hdr.target_payload or {}
        if b.history_rule and eb.table == "LIKP" and payload.get("WADAT_IST"):
            hdr.result = LoadResult("UNSUPPORTED", "", "API", b.service, b.history_rule, hdr.target_key)
            for it in items:
                it.result = LoadResult("UNSUPPORTED", "", "API", b.service, b.history_rule, it.target_key)
            return
        entity = eb.to_entity(payload)
        nav_items: dict[str, list[dict]] = defaultdict(list)
        for it in items:
            ib = b.entity_for(it.table)
            if ib is None or it.op != "I":
                continue
            nav_items[f"to_{ib.entity_set[2:] if ib.entity_set.startswith('A_') else ib.entity_set}"].append({k: v for k, v in ib.to_entity(it.target_payload or {}).items() if k not in ib.parent_props})
        entity.update(nav_items)
        d = self.client.create(b.service, eb.entity_set, entity)
        row = eb.to_row(d) | {f: d.get(p) for f, p in eb.fields.items() if f in eb.derived and d.get(p) is not None}
        stored = {**payload, **row}
        assigned = record_key(eb.table, stored)
        ext = sum(1 for k in entity if k.startswith("YY1_"))
        hdr.result = LoadResult("APPLIED", "INSERTED", "API", f"{b.service} POST {eb.entity_set}" + (" (deep insert)" if nav_items else ""), "" if assigned == hdr.target_key else f"target assigned {assigned} (proposed {hdr.target_key})", assigned, stored, ext)
        for it in items:
            ib = b.entity_for(it.table)
            if ib is None or it.op != "I":
                it.result = it.result or LoadResult("UNSUPPORTED", "", "API", b.service, f"{it.table}: {it.op} inside a create is not an API operation", it.target_key)
                continue
            irow = dict(it.target_payload or {})
            for f, p in ib.fields.items():
                if p in ib.parent_props:
                    irow[f] = stored.get(next(hf for hf, hp in eb.fields.items() if hp == p), irow.get(f)) if p in eb.fields.values() else irow.get(f)
            tkey = record_key(it.table, irow)
            trow = self._read_row(it.table, tkey) if self._read_row else irow
            it.result = LoadResult("APPLIED", "INSERTED", "API", f"{b.service} POST {eb.entity_set} (deep insert)", "", tkey, trow or irow)

    def _item_op(self, b: ApiBinding, e: EventView) -> None:
        ib = b.entity_for(e.table)
        if ib is None:
            e.result = LoadResult("UNSUPPORTED", "", "API", b.service, f"{e.table} is not exposed by {b.service} ({b.notes or 'history / derived table'})", e.target_key)
            return
        if e.op == "I":
            entity = ib.to_entity(e.target_payload or {})
            d = self.client.create(b.service, ib.entity_set, entity)
            row = {**(e.target_payload or {}), **ib.to_row(d)}
            tkey = record_key(e.table, row)
            e.result = LoadResult("APPLIED", "INSERTED", "API", f"{b.service} POST {ib.entity_set}", "", tkey, (self._read_row(e.table, tkey) if self._read_row else None) or row, sum(1 for k in entity if k.startswith("YY1_")))
        elif e.op == "D":
            vals = (e.target_key or "").split("|")
            current, etag = self.client.get(b.service, ib.entity_set, ib.key_props, vals)
            if current is None:
                e.result = LoadResult("SKIPPED_MISSING", "", "API", f"{b.service} DELETE {ib.entity_set}", "already absent from target", e.target_key)
                return
            self.client.delete(b.service, ib.entity_set, ib.key_props, vals, etag)
            e.result = LoadResult("APPLIED", "DELETED", "API", f"{b.service} DELETE {ib.entity_set}", "", e.target_key)
        else:
            self._update_entity(b, ib, e)

    def _update_entity(self, b: ApiBinding, eb: EntityBinding, e: EventView) -> None:
        vals = (e.target_key or "").split("|")
        current, etag = self.client.get(b.service, eb.entity_set, eb.key_props, vals)
        if current is None:
            e.result = LoadResult("SKIPPED_MISSING", "", "API", f"{b.service} PATCH {eb.entity_set}", f"{eb.entity_set} {e.target_key} does not exist in the target; the create event may have been filtered or rejected", e.target_key)
            return
        cur_row = eb.to_row(current)
        skip = set(eb.derived) | set(eb.priced) | set(TABLES[eb.table].key_fields)
        patch = {eb.fields.get(f) or f"YY1_{f}": v for f, v in (e.target_payload or {}).items() if f not in skip and (f in eb.updatable or f not in eb.fields) and cur_row.get(f) != v}
        derived_only = all(f in eb.derived or f in eb.priced or cur_row.get(f) == v for f, v in (e.target_payload or {}).items())
        not_updatable = [f for f, v in (e.target_payload or {}).items() if f in eb.fields and f not in eb.updatable and f not in skip and cur_row.get(f) != v]
        if not_updatable:
            e.result = LoadResult("REJECTED_BY_TARGET", "", "API", f"{b.service} PATCH {eb.entity_set}", f"{', '.join(eb.fields[f] for f in not_updatable)} cannot be changed on an existing {eb.entity_set}; re-create the document or correct it manually", e.target_key)
            return
        if not patch:
            tkey = e.target_key
            e.result = LoadResult("APPLIED", "DERIVED" if derived_only else "UPDATED", "API", f"{b.service} GET {eb.entity_set}", "only target-derived fields differ; recomputed by the target" if derived_only else "no updatable field changed", tkey, (self._read_row(eb.table, tkey) if self._read_row else None))
            return
        self.client.update(b.service, eb.entity_set, eb.key_props, vals, patch, etag)
        e.result = LoadResult("APPLIED", "UPDATED", "API", f"{b.service} PATCH {eb.entity_set}", "", e.target_key, (self._read_row(eb.table, e.target_key) if self._read_row else None), sum(1 for k in patch if k.startswith("YY1_")))

    def _delete_document(self, b: ApiBinding, hdr: EventView, items: list[EventView]) -> None:
        eb = b.header
        vals = (hdr.target_key or "").split("|")
        current, etag = self.client.get(b.service, eb.entity_set, eb.key_props, vals)
        if current is None:
            hdr.result = LoadResult("SKIPPED_MISSING", "", "API", f"{b.service} DELETE {eb.entity_set}", "already absent from target", hdr.target_key)
            for it in items:
                it.result = LoadResult("SKIPPED_MISSING", "", "API", b.service, "header already absent", it.target_key)
            return
        if b.on_delete == "DELETE":
            self.client.delete(b.service, eb.entity_set, eb.key_props, vals, etag)
            hdr.result = LoadResult("APPLIED", "DELETED", "API", f"{b.service} DELETE {eb.entity_set}", "", hdr.target_key)
            for it in items:
                it.result = LoadResult("APPLIED", "DELETED", "API", f"{b.service} DELETE {eb.entity_set} (cascade)", "", it.target_key)
        elif b.on_delete == "BLOCK":
            self.client.update(b.service, eb.entity_set, eb.key_props, vals, {"YY1_IS_BLOCKED": True}, etag)
            hdr.result = LoadResult("APPLIED", "BLOCKED", "API", f"{b.service} PATCH {eb.entity_set}", "masters are never deleted through the API: blocked for posting instead", hdr.target_key)
            for it in items:
                it.result = LoadResult("APPLIED", "BLOCKED", "API", f"{b.service} PATCH {eb.entity_set}", "blocked with its master", it.target_key)
        else:
            hdr.result = LoadResult("UNSUPPORTED", "", "API", b.service, f"{eb.entity_set} cannot be deleted or blocked through {b.service}; manual action required", hdr.target_key)
            for it in items:
                it.result = LoadResult("UNSUPPORTED", "", "API", b.service, "header cannot be deleted", it.target_key)

    # -- journal entries
    def _load_journal(self, b: ApiBinding, events: list[EventView]) -> None:
        hdr_events = [e for e in events if e.table == "BKPF"]
        line_events = [e for e in events if e.table == "BSEG"]
        derived = [e for e in events if e.table in ("BSID", "BSIK")]
        hdr = hdr_events[0] if hdr_events else None
        if hdr is None:
            for e in events:
                e.result = LoadResult("UNSUPPORTED", "", "API", b.service, "line-level changes without their document are clearing / correction postings, which this build does not generate (plan a clearing run)", e.target_key)
            return
        if hdr.op == "D":
            cc, doc, fy = (hdr.target_key or "||").split("|")[:3]
            d = self.client.reverse_journal_entry(cc, doc, fy)
            rev_key = f"{d.get('CompanyCode')}|{d.get('AccountingDocument')}|{d.get('FiscalYear')}"
            for e in events:
                e.result = LoadResult("APPLIED", "REVERSED", "API", f"{b.service} JournalEntryReverse", f"accounting documents are never deleted: reversed by {d.get('AccountingDocument')}", rev_key if e.table == "BKPF" else e.target_key)
            return
        if hdr.op == "U":
            cc, doc, fy = (hdr.target_key or "||").split("|")[:3]
            self.client.reverse_journal_entry(cc, doc, fy)
            action = "REPOSTED"
        else:
            action = "INSERTED"
        payload = b.header.to_entity(hdr.target_payload or {})
        payload["AccountingDocument"] = (hdr.target_payload or {}).get("BELNR") if b.numbering == "external" else None
        payload["Items"] = [b.items["BSEG"].to_entity({**(e.target_payload or {}), "BELNR": None}) for e in sorted(line_events, key=lambda x: int((x.target_payload or {}).get("BUZEI") or 0))]
        d = self.client.post_journal_entry(payload)
        assigned_doc = str(d.get("AccountingDocument"))
        new_hdr = {**(hdr.target_payload or {}), "BELNR": assigned_doc, "GJAHR": d.get("FiscalYear", (hdr.target_payload or {}).get("GJAHR"))}
        hkey = record_key("BKPF", new_hdr)
        hdr.result = LoadResult("APPLIED", action, "API", f"{b.service} JournalEntryBulkCreateRequestConfirmation_In", "" if hkey == hdr.target_key else f"target assigned document {assigned_doc} (proposed {(hdr.target_payload or {}).get('BELNR')})", hkey, (self._read_row("BKPF", hkey) if self._read_row else None) or new_hdr)
        for e in line_events:
            row = {**(e.target_payload or {}), "BELNR": assigned_doc}
            k = record_key("BSEG", row)
            e.result = LoadResult("APPLIED", action, "API", f"{b.service} JournalEntryBulkCreateRequestConfirmation_In (item)", "", k, (self._read_row("BSEG", k) if self._read_row else None) or row)
        for e in derived:
            row = {**(e.target_payload or {}), "BELNR": assigned_doc}
            k = record_key(e.table, row)
            e.result = LoadResult("APPLIED", "DERIVED", "API", f"{b.service} (open item derived from customer/supplier line)", "", k, (self._read_row(e.table, k) if self._read_row else None) or row)
