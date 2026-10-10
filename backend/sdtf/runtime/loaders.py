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
from ..catalog.business_objects import BUSINESS_OBJECTS, header_siblings, load_methods_for, matches_header
from ..catalog.tables import TABLES, record_key
from .target_api import ApiError, TargetApiClient

_TABLE_BO: dict[str, str] = {}
for _bo in BUSINESS_OBJECTS.values():
    _TABLE_BO.setdefault(_bo.header_table, _bo.id)
    for _t in _bo.item_tables:
        _TABLE_BO.setdefault(_t, _bo.id)


def object_of(table: str, record_key: str, payload: dict | None = None) -> tuple[str, str]:
    """(business object type, instance key) of a row. Item tables whose primary key does not start with the
    object's key (open items BSID/BSIK) need the row image; without one the positional prefix is used. A header
    table shared by several types (EKKO) is typed by the header image; its item rows carry no category and get
    the first type, which `regroup_by_header` corrects once the header of the same key is known."""
    bo_id = _TABLE_BO.get(table, "")
    if not bo_id:
        return "", record_key
    if payload and BUSINESS_OBJECTS[bo_id].header_table == table and len(header_siblings(bo_id)) > 1:
        bo_id = next((sib for sib in header_siblings(bo_id) if matches_header(BUSINESS_OBJECTS[sib], payload)), bo_id)
    bo = BUSINESS_OBJECTS[bo_id]
    td = TABLES.get(table)
    if td is not None and tuple(td.key_fields[: len(bo.key_fields)]) == tuple(bo.key_fields):
        return bo_id, "|".join(record_key.split("|")[: len(bo.key_fields)])
    if payload and all(k in payload for k in bo.key_fields):
        return bo_id, "|".join(str(payload[k]) for k in bo.key_fields)
    return bo_id, "|".join(record_key.split("|")[: len(bo.key_fields)])


def regroup_by_header(groups: dict, order: list | None = None) -> tuple[dict, list | None]:
    """Groups keyed `(object type, instance key)` of staged rows: rows of a shared header table resolve to their
    type by the header image, their item rows land on the first type of the table; every group without a header
    row moves to the sibling type whose group of the same key holds the header. Members need `.table_name`."""
    def has_header(g) -> bool:
        return any(r.table_name == BUSINESS_OBJECTS[g[0]].header_table for r in groups[g])

    moved: dict = {}
    for g in list(groups):
        if g[0] not in BUSINESS_OBJECTS:
            continue
        sibs = header_siblings(g[0])
        if len(sibs) <= 1 or has_header(g):
            continue
        target = next((s for s in sibs if s != g[0] and (s, g[1]) in groups and has_header((s, g[1]))), None)
        if target is not None:
            groups[(target, g[1])].extend(groups.pop(g))
            moved[g] = (target, g[1])
    if order is not None and moved:
        order = [g for g in order if g not in moved]
    return groups, order


def plan_cockpit(object_type: str, events: list, product: str = "S4HANA") -> tuple[list[tuple[list, str]], list]:
    """Initial-load routing of one business object instance: `([(rows, label), ...], rows_for_the_api_path)`.

    Rows go to the migration cockpit when the registry prescribes it for the object, when the object's API does not
    expose their table (PO history, production order components and confirmations) or when the instance is a
    history (completed sales order, fully delivered purchase order, goods-issued delivery) whose status the APIs
    cannot set. Everything else (API objects, configuration, unsupported objects) is returned on the second side.
    """
    if not events:
        return [], []
    bo = BUSINESS_OBJECTS.get(object_type)
    methods = load_methods_for(bo, product) if bo is not None else []
    method, api = (methods[0].method, methods[0].api) if methods else ("UNKNOWN", "")
    if method == "MIGRATION_COCKPIT":
        return [(list(events), api or "migration object")], []
    if method != "API":
        return [], list(events)
    b = binding_for(object_type)
    if b is None:
        return [], list(events)
    groups: list[tuple[list, str]] = []
    unbound = [e for e in events if b.entity_for(e.table) is None]
    rest = [e for e in events if b.entity_for(e.table) is not None]
    if unbound:
        groups.append((unbound, f"{b.service} does not expose {', '.join(sorted({e.table for e in unbound}))}: history tables through the migration cockpit"))
    hdr = next((e for e in rest if e.table == b.header.table), None)
    if hdr is not None and b.history_when is not None and b.history_when(hdr.target_payload or {}, [e.target_payload or {} for e in rest if e.table != b.header.table]):
        groups.append((rest, f"historical {b.header.entity_set} ({b.history_rule})"))
        rest = []
    return groups, rest


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
    def __init__(self, client: TargetApiClient, target_product: str = "S4HANA", read_row=None, source_ref_prefix: str = ""):
        self.client = client
        self.product = target_product
        self._read_row = read_row  # optional direct row reader (simulated gateway) for parity checks
        self.source_ref_prefix = source_ref_prefix  # identifies the sending system in idempotency references (several sources may share keys)

    def source_ref(self, record_key: str) -> str:
        return f"{self.source_ref_prefix}:{record_key}" if self.source_ref_prefix else record_key

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

    def cockpit_plan(self, object_type: str, events: list[EventView]) -> tuple[list[tuple[list[EventView], str]], list[EventView]]:
        """Which rows of one business object instance the *initial* load hands to the migration cockpit, with the
        reason label, and which continue on the API path. One decision shared by the LOAD stage and the staging-file
        export, so both agree: cockpit-only objects, tables the document APIs do not expose, and histories."""
        return plan_cockpit(object_type, events, self.product)

    # ------------------------------------------------------------------------------------- execution
    def load_change_set(self, events: list[EventView], initial: bool = False) -> None:
        """Apply the events of one change set for one business object instance. Sets `result` on each event.
        `initial`: the initial load may use the migration cockpit for cockpit objects, histories and tables the
        document APIs do not expose; delta cycles report those as unsupported."""
        if not events:
            return
        object_type = events[0].object_type
        method, api, note = self.method_for(object_type)
        if method == "CONFIG_TRANSPORT":
            for e in events:
                exists = self._exists(e.table, e.target_key) if e.target_key else False
                e.result = LoadResult("MATCHED" if exists else "CONFIG_MISSING", "MATCHED" if exists else "", method, "", "configuration comes from the target shell; record matched, not loaded" if exists else "configuration object missing in the target shell: transport it, the load cannot create it", e.target_key)
            return
        if initial:
            cockpit, events = self.cockpit_plan(object_type, events)
            for group, label in cockpit:
                self._cockpit(object_type, group, label)
            if not events:
                return
        if method == "MIGRATION_COCKPIT":
            for e in events:
                e.result = LoadResult("UNSUPPORTED", "", method, "", f"{object_type} is loaded through the migration cockpit ({api or note or 'see registry'}); changes after the snapshot are not re-posted per event and must be re-migrated at the final delta", e.target_key)
            return
        if method != "API":
            for e in events:
                e.result = LoadResult("UNSUPPORTED", "", method, "", f"{object_type}: {note or 'no supported load method'} ({method.lower().replace('_', ' ')})", e.target_key)
            return
        b = binding_for(object_type)
        if b is None:
            for e in events:
                e.result = LoadResult("UNSUPPORTED", "", method, api, f"{api or object_type} has no API binding yet; add one in catalog/api_bindings.py", e.target_key)
            return
        try:
            if b.protocol == "SOAP":
                self._load_journal(b, events, initial=initial)
            else:
                self._load_odata(b, events)
        except ApiError as ex:
            for e in events:
                if e.result is None:
                    e.result = LoadResult("REJECTED_BY_TARGET", "", "API", f"{b.service}", f"{ex.status} {ex.code}: {ex.message}", e.target_key)

    def _cockpit(self, object_type: str, events: list[EventView], label: str) -> None:
        """Migration cockpit path (initial load only): rows posted as staging-table content. Rows the target already
        holds are duplicates (identical) or conflicts (different) and are never posted again."""
        new: list[EventView] = []
        for e in events:
            if e.op != "I" or not e.target_payload:
                e.result = LoadResult("UNSUPPORTED", "", "MIGRATION_COCKPIT", label, "the migration cockpit loads records once; changes and deletions after the snapshot are not replayed", e.target_key)
                continue
            cur = self._read_row(e.table, e.target_key) if self._read_row else None
            if cur is None:
                new.append(e)
            elif all(cur.get(k) == v for k, v in e.target_payload.items()):
                e.result = LoadResult("SKIPPED_DUPLICATE", "", "MIGRATION_COCKPIT", f"MIGRATION_COCKPIT {object_type} (exists)", "target already holds this content", e.target_key, cur)
            else:
                e.result = LoadResult("CONFLICT", "", "MIGRATION_COCKPIT", f"MIGRATION_COCKPIT {object_type} (exists)", "target key already exists with different content (duplicate detection)", e.target_key, cur)
        try:
            if new:
                self.client.cockpit_load(object_type, [(e.table, dict(e.target_payload)) for e in new])
            for e in new:
                e.result = LoadResult("APPLIED", "INSERTED", "MIGRATION_COCKPIT", f"MIGRATION_COCKPIT POST {object_type} ({label})", "", e.target_key, (self._read_row(e.table, e.target_key) if self._read_row else None) or e.target_payload)
        except ApiError as ex:
            for e in new:
                e.result = LoadResult("REJECTED_BY_TARGET", "", "MIGRATION_COCKPIT", f"MIGRATION_COCKPIT POST {object_type}", f"{ex.status} {ex.code}: {ex.message}", e.target_key)

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
        if b.history_when is not None and b.history_when(payload, [i.target_payload or {} for i in items]):
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
        try:
            d = self.client.create(b.service, eb.entity_set, entity)
        except ApiError as ex:
            if ex.status != 409:
                raise
            self._classify_existing(b, hdr, items)
            return
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

    def _classify_existing(self, b: ApiBinding, hdr: EventView, items: list[EventView]) -> None:
        """The document already exists in the target (a re-run or a second partition): identical content is a
        duplicate, different content a conflict. Nothing is written."""
        for e in [hdr, *items]:
            cur = self._read_row(e.table, e.target_key) if self._read_row else None
            eb = b.entity_for(e.table)
            if cur is None and eb is not None and self._read_row is None:
                got, _ = self.client.get(b.service, eb.entity_set, eb.key_props, (e.target_key or "").split("|"))
                cur = eb.to_row(got) if got else None
            if cur is None:
                e.result = LoadResult("REJECTED_BY_TARGET", "", "API", f"{b.service} POST {b.header.entity_set}", "409 DUPLICATE: header exists but this row does not", e.target_key)
                continue
            ignore = set(eb.derived) | set(eb.priced) if eb else set()
            expected = {k: v for k, v in (e.target_payload or {}).items() if k not in ignore}
            same = all(cur.get(k) == v for k, v in expected.items())
            if same:
                e.result = LoadResult("SKIPPED_DUPLICATE", "", "API", f"{b.service} GET {eb.entity_set if eb else e.table}", "target already holds this content", e.target_key, cur)
            else:
                e.result = LoadResult("CONFLICT", "", "API", f"{b.service} GET {eb.entity_set if eb else e.table}", "target key already exists with different content (duplicate detection)", e.target_key, cur)

    def _item_op(self, b: ApiBinding, e: EventView) -> None:
        ib = b.entity_for(e.table)
        if ib is None:
            e.result = LoadResult("UNSUPPORTED", "", "API", b.service, f"{e.table} is not exposed by {b.service} ({b.notes or 'history / derived table'})", e.target_key)
            return
        if e.op == "I":
            entity = ib.to_entity(e.target_payload or {})
            try:
                d = self.client.create(b.service, ib.entity_set, entity)
            except ApiError as ex:
                if ex.status != 409:
                    raise
                self._classify_existing(b, e, [])
                return
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
    def _load_journal(self, b: ApiBinding, events: list[EventView], initial: bool = False) -> None:
        hdr_events = [e for e in events if e.table == "BKPF"]
        line_events = [e for e in events if e.table == "BSEG"]
        derived = [e for e in events if e.table in ("BSID", "BSIK")]
        hdr = hdr_events[0] if hdr_events else None
        if hdr is not None and hdr.op == "I":
            p = hdr.target_payload or {}
            found = self.client.lookup_journal_entry(p.get("BUKRS"), p.get("GJAHR"), self.source_ref(hdr.record_key))
            if found is not None:  # posted before (re-run / second partition): compare, never post twice
                doc = str(found["AccountingDocument"])
                for e in events:
                    row = {**(e.target_payload or {}), "BELNR": doc}
                    k = record_key(e.table, row)
                    cur = self._read_row(e.table, k) if self._read_row else row
                    same = cur is not None and all(cur.get(f) == v for f, v in row.items() if f not in ("BELNR", "MONAT"))
                    e.result = LoadResult("SKIPPED_DUPLICATE" if same else "CONFLICT", "", "API", f"{b.service} JournalEntryLookup", "target already holds this journal entry" if same else "journal entry exists with different content", k, cur)
                return
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
        payload["SourceReference"] = self.source_ref(hdr.record_key)
        hints: dict[int, tuple[str, dict]] = {}
        for e in derived:
            r = e.target_payload or {}
            known = {"BUKRS", "KUNNR", "LIFNR", "UMSKS", "UMSKZ", "AUGDT", "AUGBL", "ZUONR", "GJAHR", "BELNR", "BUZEI", "DMBTR", "SHKZG"}
            hints[int(r.get("BUZEI") or 0)] = ("CustomerOpenItem" if e.table == "BSID" else "SupplierOpenItem", {"Partner": r.get("KUNNR") or r.get("LIFNR"), "SpecialGLTransactionType": r.get("UMSKS", ""), "SpecialGLCode": r.get("UMSKZ", ""), "ClearingDate": r.get("AUGDT", ""), "ClearingDocument": r.get("AUGBL", ""), "AssignmentReference": r.get("ZUONR", ""), "Amount": r.get("DMBTR"), "DebitCreditCode": r.get("SHKZG"), "Extension": {f: v for f, v in r.items() if f not in known}})
        payload["Items"] = []
        for e in sorted(line_events, key=lambda x: int((x.target_payload or {}).get("BUZEI") or 0)):
            item = b.items["BSEG"].to_entity({**(e.target_payload or {}), "BELNR": None})
            h = hints.get(int((e.target_payload or {}).get("BUZEI") or 0))
            if h:
                item[h[0]] = h[1]
            payload["Items"].append(item)
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
