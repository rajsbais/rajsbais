"""Verification of the API bindings against a service's `$metadata` (EDMX).

The bindings in `catalog/api_bindings.py`, the journal item mapping and the read-only bindings were written from
the public API reference, not from a target. This module checks them against the real thing: the EDMX document a
target serves at `/sap/opu/odata/sap/<SERVICE>/$metadata` (or one downloaded from the SAP API hub). For every
service the platform relies on it reports the entity sets and properties it expects, which of them exist, their
EDM types and key flags, and for every missing property the closest names the service does have, so a rename
(`PriceControl` -> `PriceDeterminationControl`) is one look away. Nothing is changed automatically: a human
corrects the binding and re-runs the check.

Works for OData V2 and V4 EDMX (element names are matched by local name). The simulated gateway serves an EDMX
generated from the bindings, so on the simulator every expectation is met by construction; the check means
something only against a real target or a downloaded specification.
"""
from __future__ import annotations

import difflib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from ..catalog.api_bindings import API_BINDINGS, READ_SERVICES, EntityBinding


@dataclass
class EntityTypeInfo:
    name: str
    properties: dict[str, dict] = field(default_factory=dict)  # name -> {type, nullable, max_length}
    keys: list[str] = field(default_factory=list)


@dataclass
class Metadata:
    entity_sets: dict[str, str]  # entity set -> entity type (short name)
    types: dict[str, EntityTypeInfo]
    version: str = "?"
    namespaces: list[str] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_edmx(text: str) -> Metadata:
    """Entity sets, entity types with properties and keys, from an EDMX document (V2 or V4)."""
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as e:
        raise ValueError(f"not an EDMX document: {e}") from None
    if _local(root.tag) != "Edmx":
        raise ValueError(f"not an EDMX document (root element {_local(root.tag)})")
    md = Metadata({}, {}, root.attrib.get("Version", "?"))
    for schema in root.iter():
        if _local(schema.tag) != "Schema":
            continue
        ns = schema.attrib.get("Namespace", "")
        md.namespaces.append(ns)
        for et in schema:
            if _local(et.tag) == "EntityType":
                info = EntityTypeInfo(et.attrib["Name"])
                for child in et:
                    ln = _local(child.tag)
                    if ln == "Property":
                        info.properties[child.attrib["Name"]] = {"type": child.attrib.get("Type", ""), "nullable": child.attrib.get("Nullable", "true").lower() != "false", "max_length": child.attrib.get("MaxLength")}
                    elif ln == "Key":
                        info.keys = [pr.attrib["Name"] for pr in child if _local(pr.tag) == "PropertyRef"]
                md.types[info.name] = info
            elif _local(et.tag) == "EntityContainer":
                for es in et:
                    if _local(es.tag) == "EntitySet":
                        md.entity_sets[es.attrib["Name"]] = es.attrib.get("EntityType", "").rsplit(".", 1)[-1]
    if not md.entity_sets and not md.types:
        raise ValueError("EDMX document contains no entity sets")
    return md


# ---------------------------------------------------------------------------------- what the platform expects
def expectations(service: str) -> list[dict]:
    """Entity sets and properties the platform relies on for a service: [{entity_set, table, properties, keys}]."""
    from ..reconciliation.views import JOURNAL_ENTITY, JOURNAL_FIELDS, JOURNAL_HEADER_FIELDS, JOURNAL_SERVICE

    out: list[dict] = []

    def add(eb: EntityBinding, use: str):
        props = sorted(set(eb.fields.values()))
        out.append({"entity_set": eb.entity_set, "table": eb.table, "use": use, "properties": props, "keys": list(eb.key_props), "parent_props": list(eb.parent_props)})

    for b in API_BINDINGS.values():
        if b.service == service and b.protocol == "ODATA_V2":
            add(b.header, "load + read-back")
            for it in b.items.values():
                add(it, "load + read-back")
    for svc, sets in READ_SERVICES.items():
        if svc == service:
            for eb in sets.values():
                add(eb, "read-back only")
    if service == JOURNAL_SERVICE:
        props = sorted(set(JOURNAL_FIELDS) | set(JOURNAL_HEADER_FIELDS))
        out.append({"entity_set": JOURNAL_ENTITY, "table": "BKPF/BSEG/BSID/BSIK", "use": "read-back only", "properties": props, "keys": ["CompanyCode", "FiscalYear", "AccountingDocument", "LedgerGLLineItem"], "parent_props": [], "optional": ["AccountingDocumentItem", "LedgerGLLineItem", "SpecialGLCode", "AssignmentReference", "PartnerCompany", "ClearingAccountingDocument", "ClearingDate", "DocumentReferenceID", "OriginalReferenceDocument", "OriginalReferenceDocumentType", "Material", "Plant", "CostCenter", "ProfitCenter", "Customer", "Supplier", "DebitCreditCode", "AmountInTransactionCurrency", "TransactionCurrency", "FiscalPeriod", "DocumentDate", "AccountingDocumentType"]})
    return out


def bound_services() -> list[str]:
    from ..reconciliation.views import JOURNAL_SERVICE

    return sorted({b.service for b in API_BINDINGS.values() if b.protocol == "ODATA_V2"} | set(READ_SERVICES) | {JOURNAL_SERVICE})


def check_service(md: Metadata, service: str) -> dict:
    """Compare the platform's expectations for `service` with its metadata."""
    exp = expectations(service)
    if not exp:
        return {"service": service, "verdict": "NOT_BOUND", "entity_sets": [], "note": "the platform does not rely on this service"}
    sets_out = []
    missing_total = 0
    for e in exp:
        es = e["entity_set"]
        optional = set(e.get("optional", ()))
        if es not in md.entity_sets:
            close = difflib.get_close_matches(es, list(md.entity_sets), n=3, cutoff=0.5)
            sets_out.append({**e, "found": False, "closest_entity_sets": close, "properties_found": [], "properties_missing": e["properties"], "property_types": {}, "keys_in_service": [], "key_match": False})
            missing_total += len([p for p in e["properties"] if p not in optional])
            continue
        info = md.types.get(md.entity_sets[es])
        have = info.properties if info else {}
        found = [p for p in e["properties"] if p in have]
        missing = [p for p in e["properties"] if p not in have]
        suggestions = {p: difflib.get_close_matches(p, list(have), n=3, cutoff=0.6) for p in missing}
        keys = info.keys if info else []
        key_match = list(e["keys"]) == keys if e["keys"] else True
        extra = sorted(set(have) - set(e["properties"]))
        hard_missing = [p for p in missing if p not in optional]
        missing_total += len(hard_missing)
        sets_out.append({**e, "found": True, "entity_type": md.entity_sets[es], "properties_found": found, "properties_missing": missing, "properties_missing_required": hard_missing, "suggestions": suggestions, "property_types": {p: have[p] for p in found}, "keys_in_service": keys, "key_match": key_match, "other_properties": len(extra)})
    sets_missing = [s["entity_set"] for s in sets_out if not s["found"]]
    key_issues = [s["entity_set"] for s in sets_out if s["found"] and not s["key_match"]]
    verdict = "VERIFIED" if not sets_missing and missing_total == 0 and not key_issues else ("DEVIATIONS" if not sets_missing else "ENTITY_SETS_MISSING")
    return {"service": service, "verdict": verdict, "edmx_version": md.version, "entity_sets": sets_out, "entity_sets_missing": sets_missing, "properties_missing": missing_total, "key_issues": key_issues}


def edmx_from_bindings(service: str) -> str:
    """The EDMX the simulated gateway serves: generated from the bindings, so it says exactly what the simulator
    answers (not what a real target has)."""
    from ..reconciliation.views import JOURNAL_ENTITY, JOURNAL_FIELDS, JOURNAL_HEADER_FIELDS, JOURNAL_SERVICE

    types: list[tuple[str, list[str], list[tuple[str, str]]]] = []  # (name, keys, [(prop, type)])
    for e in expectations(service):
        if service == JOURNAL_SERVICE and e["entity_set"] == JOURNAL_ENTITY:
            props = [(p, "Edm.Decimal" if p.startswith("Amount") else "Edm.String") for p in sorted(set(JOURNAL_FIELDS) | set(JOURNAL_HEADER_FIELDS))]
        else:
            props = [(p, "Edm.String") for p in e["properties"]]
        types.append((e["entity_set"], e["keys"], props))
    if not types:
        raise ValueError(f"service {service} is not bound")
    body = []
    for name, keys, props in types:
        key_xml = "".join(f'<PropertyRef Name="{k}"/>' for k in keys)
        prop_xml = "".join(f'<Property Name="{p}" Type="{t}" Nullable="{"false" if p in keys else "true"}"/>' for p, t in props)
        body.append(f'<EntityType Name="{name}Type"><Key>{key_xml}</Key>{prop_xml}</EntityType>')
    sets = "".join(f'<EntitySet Name="{name}" EntityType="{service}.{name}Type"/>' for name, _k, _p in types)
    return f'<?xml version="1.0" encoding="utf-8"?><edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"><edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" m:DataServiceVersion="2.0"><Schema Namespace="{service}" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">{"".join(body)}<EntityContainer Name="{service}_Entities" m:IsDefaultEntityContainer="true">{sets}</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>'


def check_target(transport, services: list[str] | None = None) -> dict:
    """Fetch the catalogue and the `$metadata` of every bound service from a target transport and check them."""
    services = services or bound_services()
    try:
        catalog = transport.catalog()
    except Exception as e:  # noqa: BLE001
        catalog = None
        catalog_error = str(e)
    else:
        catalog_error = ""
    out = {"transport": getattr(transport, "name", "?"), "catalog_available": catalog is not None, "catalog_error": catalog_error, "services": []}
    for svc in services:
        entry = {"service": svc, "activated": (svc in catalog) if catalog is not None else None}
        try:
            md = parse_edmx(transport.metadata(svc))
            entry.update(check_service(md, svc))
        except Exception as e:  # noqa: BLE001
            entry.update({"verdict": "UNAVAILABLE", "error": str(e)[:300]})
        out["services"].append(entry)
    out["summary"] = {v: sum(1 for s in out["services"] if s.get("verdict") == v) for v in ("VERIFIED", "DEVIATIONS", "ENTITY_SETS_MISSING", "UNAVAILABLE", "NOT_BOUND")}
    return out


def report_markdown(result: dict) -> str:
    lines = [f"# API metadata check ({result.get('transport', 'file')})", ""]
    if "services" in result:
        lines.append(f"Catalogue: {'read' if result.get('catalog_available') else 'not available (' + (result.get('catalog_error') or '') + ')'}; " + ", ".join(f"{k} {v}" for k, v in result["summary"].items() if v))
        services = result["services"]
    else:
        services = [result]
    for s in services:
        lines += ["", f"## {s['service']}: {s.get('verdict')}" + ("" if s.get("activated") is None else (" (activated)" if s["activated"] else " (not in the catalogue)"))]
        if s.get("error"):
            lines.append(f"error: {s['error']}")
        for e in s.get("entity_sets", []):
            if not e["found"]:
                lines.append(f"* `{e['entity_set']}` ({e['table']}): **missing**" + (f"; closest: {', '.join(e['closest_entity_sets'])}" if e.get("closest_entity_sets") else ""))
                continue
            lines.append(f"* `{e['entity_set']}` ({e['table']}, {e['use']}): {len(e['properties_found'])}/{len(e['properties'])} properties found, keys {'match' if e['key_match'] else 'differ: ' + ', '.join(e['keys_in_service'])}")
            for p in e["properties_missing"]:
                sug = e["suggestions"].get(p) or []
                lines.append(f"  * missing `{p}`" + (" (optional)" if p not in e.get("properties_missing_required", e["properties_missing"]) else "") + (f": closest {', '.join('`' + x + '`' for x in sug)}" if sug else ""))
    return "\n".join(lines) + "\n"


__all__ = ["parse_edmx", "expectations", "bound_services", "check_service", "check_target", "edmx_from_bindings", "report_markdown", "Metadata"]
