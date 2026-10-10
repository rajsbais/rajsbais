"""Scope selection, recursive dependency expansion and dependency-complete plan generation."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..sap.adapter import Row, SourceAdapter
from ..sap.connectors.rfc import RemoteError
from ..sap.ddic import TABLES
from ..selective.manifest import Manifest
from .registry import CONFIG_TYPES, Registry, RelKind, find_cycles


@dataclass
class Issue:
    code: str
    severity: str  # blocking | warning | info
    message: str
    instance: str | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self):
        return {"code": self.code, "severity": self.severity, "message": self.message,
                "instance": self.instance, "details": self.details}


@dataclass
class PlanInstance:
    id: str
    type: str
    key: str
    origin: str  # ROOT | REQUIRED | DOWNSTREAM
    parent: str | None = None
    rows: dict[str, list[Row]] = field(default_factory=dict)
    requires: list[str] = field(default_factory=list)
    configs: list[str] = field(default_factory=list)  # e.g. PLANT:2000

    def row_counts(self) -> dict[str, int]:
        return {t: len(r) for t, r in self.rows.items()}


@dataclass
class Plan:
    manifest_hash: str
    instances: dict[str, PlanInstance]
    order: list[str]
    config_refs: dict[str, set[str]]  # COMPANY_CODE / PLANT -> codes needed
    issues: list[Issue]
    cycles: list[list[str]]

    @property
    def blocking(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "blocking"]

    def table_rows(self, only: set[str] | None = None) -> dict[str, int]:
        out: dict[str, int] = {}
        for iid, inst in self.instances.items():
            if only is not None and iid not in only:
                continue
            for t, rows in inst.rows.items():
                out[t] = out.get(t, 0) + len(rows)
        return out

    def volume_bytes(self, only: set[str] | None = None) -> int:
        n = 0
        for iid, inst in self.instances.items():
            if only is not None and iid not in only:
                continue
            for rows in inst.rows.values():
                for r in rows:
                    n += len(json.dumps(r, separators=(",", ":")))
        return n

    def summary(self) -> dict:
        by_type: dict[str, int] = {}
        by_origin: dict[str, int] = {}
        for i in self.instances.values():
            by_type[i.type] = by_type.get(i.type, 0) + 1
            by_origin[i.origin] = by_origin.get(i.origin, 0) + 1
        return {
            "manifest_hash": self.manifest_hash,
            "instances": len(self.instances),
            "by_type": by_type, "by_origin": by_origin,
            "tables": self.table_rows(),
            "total_rows": sum(self.table_rows().values()),
            "bytes": self.volume_bytes(),
            "config_prerequisites": {k: sorted(v) for k, v in self.config_refs.items()},
            "issues": [i.to_dict() for i in self.issues],
            "blocking": len(self.blocking) > 0,
            "cycles": self.cycles,
        }


def key_of_header(ot, row: Row) -> str:
    return "/".join(str(row[k]) for k in ot.header_keys)


class Planner:
    def __init__(self, reader: SourceAdapter, registry: Registry):
        self.r = reader
        self.reg = registry

    # ---- root selection ------------------------------------------------
    def select_roots(self, m: Manifest, issues: list[Issue]) -> list[str]:
        ot = self.reg.types.get(m.scope.object_type)
        if ot is None:
            issues.append(Issue("UNKNOWN_OBJECT_TYPE", "blocking", f"Unknown object type {m.scope.object_type}"))
            return []
        keysets: list[set[str]] = []
        for dim, values in m.scope.dims().items():
            spec = ot.filters.get(dim)
            if spec is None:
                issues.append(Issue("UNSUPPORTED_FILTER", "blocking",
                                    f"Filter '{dim}' is not supported for {ot.name}",
                                    details={"supported": sorted(ot.filters)}))
                continue
            table, fld = spec
            vals = set(values)
            rows = self.r.select_in(table, fld, vals) if hasattr(self.r, "select_in") else self.r.select(table, lambda x: x.get(fld) in vals)
            keysets.append({key_of_header(ot, r) for r in rows})
        if m.scope.date_from or m.scope.date_to:
            if ot.date_field is None:
                issues.append(Issue("UNSUPPORTED_FILTER", "blocking", f"{ot.name} has no date field"))
            else:
                table, fld = ot.date_field
                lo = m.scope.date_from.isoformat() if m.scope.date_from else "0000-00-00"
                hi = m.scope.date_to.isoformat() if m.scope.date_to else "9999-12-31"
                rows = (self.r.select_between(table, fld, lo, hi) if hasattr(self.r, "select_between")
                        else self.r.select(table, lambda x: lo <= x.get(fld, "") <= hi))
                keysets.append({key_of_header(ot, r) for r in rows})
        if m.scope.explicit_keys:
            keysets.append(set(m.scope.explicit_keys))
        if not keysets:
            return [key_of_header(ot, r) for r in self.r.select(ot.header) if self._is_type(ot, r)]
        keys = set.intersection(*keysets)
        if ot.header_prefix:  # item-level or shared-header hits may belong to another object type
            keys = {k for k in keys if (h := self.r.get(ot.header, tuple(k.split("/")))) is None or self._is_type(ot, h)}
        # intersect with existing headers (item-level hits always have a header, explicit keys may not)
        return sorted(keys)

    @staticmethod
    def _is_type(ot, header: Row) -> bool:
        if not ot.header_prefix:
            return True
        f, pre = ot.header_prefix
        return str(header.get(f, "")).startswith(pre)

    # ---- row collection -------------------------------------------------
    def collect(self, type_name: str, key: str, m: Manifest) -> PlanInstance | None:
        ot = self.reg.types[type_name]
        hdr = self.r.get(ot.header, tuple(key.split("/")))
        if hdr is None or not self._is_type(ot, hdr):
            return None
        rows: dict[str, list[Row]] = {ot.header: [hdr]}
        for link in ot.tables[1:]:
            parent_rows = rows.get(link.parent, [])
            seen, out = set(), []
            for pr in parent_rows:
                (cf, pf), rest = link.join[0], link.join[1:]
                if pr.get(pf) in (None, ""):
                    continue
                for c in self.r.lookup(link.table, cf, pr[pf]):
                    if any(c.get(a) != pr.get(b) for a, b in rest):
                        continue
                    if link.scope_dim == "company_codes" and m.scope.company_codes \
                            and c.get(link.scope_field) not in m.scope.company_codes:
                        continue
                    k = tuple(c[x] for x in TABLES[link.table].keys)
                    if k not in seen:
                        seen.add(k); out.append(c)
            # same table may appear under several links (ADRC / VBFA): merge
            rows.setdefault(link.table, [])
            have = {tuple(c[x] for x in TABLES[link.table].keys) for c in rows[link.table]}
            rows[link.table] += [c for c in out if tuple(c[x] for x in TABLES[link.table].keys) not in have]
        return PlanInstance(f"{type_name}:{key}", type_name, key, "ROOT", rows=rows)

    # ---- relationship extraction ---------------------------------------
    @staticmethod
    def _targets(rel, inst: PlanInstance) -> list[str]:
        out = []
        for row in inst.rows.get(rel.via_table, []):
            if any(row.get(f) != v for f, v in rel.where):
                continue
            v = row.get(rel.via_field)
            if v not in (None, "") and v not in out:
                out.append(v)
        return out

    def _downstream_instances(self, rel, dst_key: str) -> list[str]:
        src_ot = self.reg.types[rel.src]
        rows = self.r.lookup(rel.via_table, rel.via_field, dst_key)
        out = []
        for row in rows:
            if any(row.get(f) != v for f, v in rel.where):
                continue
            k = "/".join(str(row[x]) for x in src_ot.header_keys)
            if k not in out:
                out.append(k)
        return out

    # ---- plan -----------------------------------------------------------
    def build(self, m: Manifest) -> Plan:
        issues: list[Issue] = []
        reg_check = self.reg.validate()
        for c in reg_check["type_cycles"]:
            issues.append(Issue("TYPE_CYCLE", "blocking", f"Cyclic object-type dependency: {' ↔ '.join(c)}",
                                details={"cycle": c}))
        for p in reg_check["problems"]:
            issues.append(Issue("REGISTRY_INVALID", "blocking", p))

        try:
            roots = self.select_roots(m, issues)
        except RemoteError as e:
            issues.append(Issue("SOURCE_UNAVAILABLE", "blocking", f"The source cannot supply this scope: {e}"))
            roots = []
        if not roots and not any(i.severity == "blocking" for i in issues):
            issues.append(Issue("EMPTY_SCOPE", "blocking", "Scope selects no business objects"))

        instances: dict[str, PlanInstance] = {}
        queue: list[tuple[str, str, str, str | None]] = [(m.scope.object_type, k, "ROOT", None) for k in roots]
        req_rels: dict[str, list] = {}
        for rel in self.reg.relationships:
            if rel.kind == RelKind.REQUIRES:
                req_rels.setdefault(rel.src, []).append(rel)
        rev_rels: dict[str, list] = {}
        for rel in self.reg.relationships:
            if rel.kind == RelKind.REQUIRES and rel.reverse and rel.src in m.include_downstream:
                rev_rels.setdefault(rel.dst, []).append(rel)

        while queue:
            tname, key, origin, parent = queue.pop(0)
            iid = f"{tname}:{key}"
            if iid in instances:
                continue
            try:
                inst = self.collect(tname, key, m)
            except RemoteError as e:
                issues.append(Issue("SOURCE_UNAVAILABLE", "blocking", f"{tname} {key} (required by {parent}) cannot be read from the source: {e}", instance=parent))
                continue
            if inst is None:
                issues.append(Issue("DANGLING_REFERENCE", "blocking",
                                    f"{tname} {key} is required by {parent} but does not exist in the source",
                                    instance=parent, details={"missing": iid}))
                continue
            inst.origin, inst.parent = origin, parent
            instances[iid] = inst
            for rel in req_rels.get(tname, []):
                for dk in self._targets(rel, inst):
                    did = f"{rel.dst}:{dk}"
                    if did not in inst.requires:
                        inst.requires.append(did)
                    if did not in instances:
                        queue.append((rel.dst, dk, "REQUIRED", iid))
            for rel in rev_rels.get(tname, []):
                for sk in self._downstream_instances(rel, key):
                    if f"{rel.src}:{sk}" not in instances:
                        queue.append((rel.src, sk, "DOWNSTREAM", iid))

        # downstream docs found after their upstream was processed still need `requires` edges: already set in own pass.
        # material plant restriction (needs referenced plants from documents)
        referenced_plants: set[str] = set()
        for inst in instances.values():
            for rel in self.reg.relationships:
                if rel.kind == RelKind.CONFIG and rel.src == inst.type and rel.dst == "PLANT" and inst.type != "MATERIAL":
                    referenced_plants.update(self._targets(rel, inst))
        allowed_plants: set[str] | None = None
        if m.scope.plants or m.scope.company_codes:
            allowed_plants = set(m.scope.plants) | {
                p["WERKS"] for p in self.r.select("T001W") if p["BUKRS"] in m.scope.company_codes} | referenced_plants
        if allowed_plants is not None:
            for inst in instances.values():
                if inst.type == "MATERIAL" and "MARC" in inst.rows:
                    inst.rows["MARC"] = [x for x in inst.rows["MARC"] if x["WERKS"] in allowed_plants]

        # configuration prerequisites
        config_refs: dict[str, set[str]] = {c: set() for c in CONFIG_TYPES}
        for inst in instances.values():
            for rel in self.reg.relationships:
                if rel.kind == RelKind.CONFIG and rel.src == inst.type:
                    for v in self._targets(rel, inst):
                        config_refs[rel.dst].add(v)
                        ref = f"{rel.dst}:{v}"
                        if ref not in inst.configs:
                            inst.configs.append(ref)
        if m.scope.company_codes:
            for cc in sorted(config_refs["COMPANY_CODE"] - set(m.scope.company_codes)):
                users = [i.id for i in instances.values() if f"COMPANY_CODE:{cc}" in i.configs][:5]
                issues.append(Issue("CROSS_COMPANY_REFERENCE", "warning",
                                    f"Scope references company code {cc} outside the selected company codes",
                                    details={"company_code": cc, "examples": users}))
            plant_cc = {p["WERKS"]: p["BUKRS"] for p in self.r.select("T001W")}
            for pl in sorted(config_refs["PLANT"]):
                if plant_cc.get(pl) and plant_cc[pl] not in m.scope.company_codes:
                    users = [i.id for i in instances.values() if f"PLANT:{pl}" in i.configs][:5]
                    issues.append(Issue("CROSS_COMPANY_REFERENCE", "warning",
                                        f"Plant {pl} belongs to company code {plant_cc[pl]} (outside scope)",
                                        details={"plant": pl, "company_code": plant_cc[pl], "examples": users}))

        # cycles + load order
        edges = {i: {r for r in inst.requires if r in instances} for i, inst in instances.items()}
        cycles = find_cycles(edges)
        for c in cycles:
            issues.append(Issue("INSTANCE_CYCLE", "blocking", f"Circular object dependency: {' ↔ '.join(c[:6])}",
                                details={"cycle": c}))
        gaps = getattr(self.r, "gaps", None)  # sources that supply only part of the DDIC model (OData APIs)
        if gaps is not None:
            touched = {t for inst in instances.values() for t in inst.rows}
            for t, fields in sorted(gaps().items()):
                if t in touched:
                    issues.append(Issue("SOURCE_FIELD_GAP", "blocking",
                                        f"The source does not supply {t}-{', '.join(fields[:6])}: objects containing {t} cannot be copied from it faithfully",
                                        details={"table": t, "fields": fields}))
        order = self._topo(instances, edges)
        return Plan(m.content_hash(), instances, order, config_refs, issues, cycles)

    def _topo(self, instances: dict[str, PlanInstance], edges: dict[str, set[str]]) -> list[str]:
        rank = {i: (self.reg.types[inst.type].load_rank, inst.key) for i, inst in instances.items()}
        indeg = {i: len(e) for i, e in edges.items()}
        rev: dict[str, list[str]] = {}
        for i, e in edges.items():
            for d in e:
                rev.setdefault(d, []).append(i)
        ready = sorted((i for i, d in indeg.items() if d == 0), key=rank.get)
        out = []
        import heapq
        heap = [(rank[i], i) for i in ready]
        heapq.heapify(heap)
        while heap:
            _, i = heapq.heappop(heap)
            out.append(i)
            for n in rev.get(i, []):
                indeg[n] -= 1
                if indeg[n] == 0:
                    heapq.heappush(heap, (rank[n], n))
        out += sorted((i for i in instances if i not in set(out)), key=rank.get)  # cyclic remainder (already blocked)
        return out

    # ---- source relationship validation ---------------------------------
    def validate_relationships(self, sample: int = 10) -> dict:
        """Business-relationship validation across the whole source: orphans + dangling references."""
        orphans, dangling = [], []
        for ot in self.reg.types.values():
            for link in ot.tables[1:]:
                if link.table in ("ADRC", "VBFA", "BUT000"):  # shared tables: checked separately below
                    continue
                for row in self.r.select(link.table):
                    (cf, pf), rest = link.join[0], link.join[1:]
                    parents = [p for p in self.r.lookup(link.parent, pf, row.get(cf))
                               if all(p.get(b) == row.get(a) for a, b in rest)]
                    if not parents:
                        orphans.append({"table": link.table, "key": [row[k] for k in TABLES[link.table].keys]})
        if "BUT000" in TABLES:  # business partner must belong to a customer or a vendor (CVI)
            for bp in self.r.select("BUT000"):
                if not self.r.lookup("KNA1", "KUNNR", bp["PARTNER"]) and not self.r.lookup("LFA1", "LIFNR", bp["PARTNER"]):
                    orphans.append({"table": "BUT000", "key": [bp["PARTNER"]]})
        for rel in self.reg.relationships:
            if rel.kind != RelKind.REQUIRES or rel.dst not in self.reg.types:
                continue
            dot = self.reg.types[rel.dst]
            for row in self.r.select(rel.via_table):
                if any(row.get(f) != v for f, v in rel.where):
                    continue
                v = row.get(rel.via_field)
                if v in (None, ""):
                    continue
                if self.r.get(dot.header, tuple(str(v).split("/"))) is None:
                    dangling.append({"relationship": rel.name, "table": rel.via_table, "value": v})
        return {"orphans": len(orphans), "dangling": len(dangling),
                "orphan_samples": orphans[:sample], "dangling_samples": dangling[:sample]}
