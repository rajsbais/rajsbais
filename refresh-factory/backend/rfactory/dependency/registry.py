"""Business object registry and semantic relationships.

Relationships encode *business* meaning (a delivery requires the order it fulfils; an FI
document posted from billing requires that billing document) rather than DDIC foreign keys.
The registry is extensible at runtime for custom Z/Y objects (see `register_*`).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum


@dataclass(frozen=True)
class TableLink:
    table: str
    parent: str | None  # None for the header table
    join: tuple[tuple[str, str], ...] = ()  # (child_field, parent_field)
    scope_dim: str | None = None  # restrict sub-rows to scope dimension: company_codes | plants
    scope_field: str | None = None


@dataclass(frozen=True)
class ObjectType:
    name: str
    label: str
    module: str  # FI | SD | MM | PP | MD (master data) | CUSTOM
    kind: str  # master | document
    tables: tuple[TableLink, ...]
    header_keys: tuple[str, ...]
    date_field: tuple[str, str] | None = None  # (table, field)
    filters: dict = field(default_factory=dict)  # scope dimension -> (table, field)
    load_rank: int = 50  # tie-break for load order within a dependency level

    @property
    def header(self) -> str:
        return self.tables[0].table


class RelKind(str, Enum):
    REQUIRES = "requires"  # upstream dependency, always followed
    CONFIG = "config"  # customizing prerequisite, verified in target not copied


@dataclass(frozen=True)
class Relationship:
    name: str
    src: str
    dst: str  # object type name, or COMPANY_CODE / PLANT for CONFIG
    kind: RelKind
    via_table: str
    via_field: str
    where: tuple[tuple[str, str], ...] = ()  # row predicate: field == value
    reverse: bool = False  # may be walked downstream (dst -> src) when src type is in include_downstream
    description: str = ""


def L(table, parent=None, join=(), scope_dim=None, scope_field=None):
    return TableLink(table, parent, tuple(join), scope_dim, scope_field)


OBJECT_TYPES: dict[str, ObjectType] = {o.name: o for o in [
    ObjectType("CUSTOMER", "Customer", "MD", "master",
               (L("KNA1"), L("KNB1", "KNA1", [("KUNNR", "KUNNR")], "company_codes", "BUKRS"),
                L("KNVV", "KNA1", [("KUNNR", "KUNNR")], "company_codes", "VKORG"),
                L("KNBK", "KNA1", [("KUNNR", "KUNNR")]), L("ADRC", "KNA1", [("ADDRNUMBER", "ADRNR")])),
               ("KUNNR",), None, {"company_codes": ("KNB1", "BUKRS"), "customers": ("KNA1", "KUNNR")}, 10),
    ObjectType("VENDOR", "Vendor", "MD", "master",
               (L("LFA1"), L("LFB1", "LFA1", [("LIFNR", "LIFNR")], "company_codes", "BUKRS"),
                L("LFBK", "LFA1", [("LIFNR", "LIFNR")]), L("ADRC", "LFA1", [("ADDRNUMBER", "ADRNR")])),
               ("LIFNR",), None, {"company_codes": ("LFB1", "BUKRS"), "vendors": ("LFA1", "LIFNR")}, 11),
    ObjectType("MATERIAL", "Material", "MD", "master",
               (L("MARA"), L("MAKT", "MARA", [("MATNR", "MATNR")]),
                L("MARC", "MARA", [("MATNR", "MATNR")], "plants", "WERKS")),
               ("MATNR",), ("MARA", "ERSDA"), {"plants": ("MARC", "WERKS"), "materials": ("MARA", "MATNR")}, 12),
    ObjectType("SALES_ORDER", "Sales order", "SD", "document",
               (L("VBAK"), L("VBAP", "VBAK", [("VBELN", "VBELN")])),
               ("VBELN",), ("VBAK", "ERDAT"),
               {"company_codes": ("VBAK", "BUKRS_VF"), "sales_orgs": ("VBAK", "VKORG"),
                "customers": ("VBAK", "KUNNR"), "plants": ("VBAP", "WERKS"), "materials": ("VBAP", "MATNR"),
                "document_types": ("VBAK", "AUART")}, 30),
    ObjectType("DELIVERY", "Outbound delivery", "SD", "document",
               (L("LIKP"), L("LIPS", "LIKP", [("VBELN", "VBELN")]), L("VBFA", "LIKP", [("VBELN", "VBELN")])),
               ("VBELN",), ("LIKP", "ERDAT"),
               {"customers": ("LIKP", "KUNNR"), "plants": ("LIPS", "WERKS")}, 31),
    ObjectType("BILLING", "Billing document", "SD", "document",
               (L("VBRK"), L("VBRP", "VBRK", [("VBELN", "VBELN")]), L("VBFA", "VBRK", [("VBELN", "VBELN")])),
               ("VBELN",), ("VBRK", "FKDAT"),
               {"company_codes": ("VBRK", "BUKRS"), "customers": ("VBRK", "KUNRG")}, 32),
    ObjectType("FI_DOCUMENT", "Accounting document", "FI", "document",
               (L("BKPF"), L("BSEG", "BKPF", [("BUKRS", "BUKRS"), ("BELNR", "BELNR"), ("GJAHR", "GJAHR")])),
               ("BUKRS", "BELNR", "GJAHR"), ("BKPF", "BUDAT"),
               {"company_codes": ("BKPF", "BUKRS"), "fiscal_years": ("BKPF", "GJAHR"),
                "document_types": ("BKPF", "BLART")}, 33),
    ObjectType("PURCHASE_ORDER", "Purchase order", "MM", "document",
               (L("EKKO"), L("EKPO", "EKKO", [("EBELN", "EBELN")])),
               ("EBELN",), ("EKKO", "BEDAT"),
               {"company_codes": ("EKKO", "BUKRS"), "vendors": ("EKKO", "LIFNR"),
                "plants": ("EKPO", "WERKS"), "document_types": ("EKKO", "BSART")}, 34),
    # --- manufacturing and inventory (reduced models: simplified routing/operation/confirmation keys, no work-centre master, costing or batches) ---
    ObjectType("BOM", "Bill of material", "PP", "master",
               (L("STKO"), L("STPO", "STKO", [("STLNR", "STLNR")])),
               ("STLNR",), None, {"plants": ("STKO", "WERKS"), "materials": ("STKO", "MATNR")}, 13),
    # --- SAP flight demo data model (present in ABAP trial systems such as NPL) ---
    ObjectType("CARRIER", "Airline with its flight schedule", "DEMO", "master",
               (L("SCARR"), L("SPFLI", "SCARR", [("CARRID", "CARRID")])),
               ("CARRID",), None, {"carriers": ("SCARR", "CARRID")}, 15),
    ObjectType("TRAVEL_CUSTOMER", "Flight customer", "DEMO", "master",
               (L("SCUSTOM"),), ("ID",), None, {"customers": ("SCUSTOM", "ID")}, 16),
    ObjectType("FLIGHT", "Flight with its bookings", "DEMO", "document",
               (L("SFLIGHT"), L("SBOOK", "SFLIGHT", [("CARRID", "CARRID"), ("CONNID", "CONNID"), ("FLDATE", "FLDATE")])),
               ("CARRID", "CONNID", "FLDATE"), ("SFLIGHT", "FLDATE"), {"carriers": ("SFLIGHT", "CARRID"), "customers": ("SBOOK", "CUSTOMID")}, 37),
    # --- quality management: an inspection lot with its characteristics, results and usage decision ---
    ObjectType("INSPECTION_LOT", "Inspection lot (QM)", "QM", "document",
               (L("QALS"), L("QAMV", "QALS", [("PRUEFLOS", "PRUEFLOS")]), L("QASR", "QALS", [("PRUEFLOS", "PRUEFLOS")]), L("QAVE", "QALS", [("PRUEFLOS", "PRUEFLOS")])),
               ("PRUEFLOS",), ("QALS", "ENSTEHDAT"), {"plants": ("QALS", "WERKS"), "materials": ("QALS", "MATNR")}, 38),
    ObjectType("FUNC_LOCATION", "Functional location (PM)", "PM", "master",
               (L("IFLOT"),), ("TPLNR",), None, {"plants": ("IFLOT", "SWERK")}, 36),
    ObjectType("EQUIPMENT", "Equipment (PM)", "PM", "master",
               (L("EQUI"), L("EQKT", "EQUI", [("EQUNR", "EQUNR")])),
               ("EQUNR",), None, {"plants": ("EQUI", "SWERK"), "materials": ("EQUI", "MATNR")}, 37),
    ObjectType("MAINT_NOTIFICATION", "Maintenance notification (PM)", "PM", "document",
               (L("QMEL"),), ("QMNUM",), ("QMEL", "QMDAT"), {"plants": ("QMEL", "SWERK")}, 39),
    # --- HR master data: special-category personal data (needs hr:copy; masking must be per-run anonymization) ---
    ObjectType("EMPLOYEE", "Employee (HR master data)", "HR", "master",
               (L("PA0003"), L("PA0001", "PA0003", [("PERNR", "PERNR")], "company_codes", "BUKRS"), L("PA0002", "PA0003", [("PERNR", "PERNR")]),
                L("PA0006", "PA0003", [("PERNR", "PERNR")]), L("PA0008", "PA0003", [("PERNR", "PERNR")]), L("PA0009", "PA0003", [("PERNR", "PERNR")])),
               ("PERNR",), None, {"company_codes": ("PA0001", "BUKRS"), "plants": ("PA0001", "WERKS")}, 17),
    ObjectType("ROUTING", "Routing (task list)", "PP", "master",
               (L("PLKO"), L("PLPO", "PLKO", [("PLNNR", "PLNNR")])),
               ("PLNNR",), None, {"plants": ("PLKO", "WERKS"), "materials": ("PLKO", "MATNR")}, 14),
    ObjectType("PRODUCTION_ORDER", "Production order", "PP", "document",
               (L("AUFK"), L("AFKO", "AUFK", [("AUFNR", "AUFNR")]), L("AFPO", "AUFK", [("AUFNR", "AUFNR")]), L("RESB", "AUFK", [("AUFNR", "AUFNR")]),
                L("AFVC", "AUFK", [("AUFNR", "AUFNR")]), L("AFRU", "AUFK", [("AUFNR", "AUFNR")])),
               ("AUFNR",), ("AUFK", "ERDAT"),
               {"company_codes": ("AUFK", "BUKRS"), "plants": ("AUFK", "WERKS"), "materials": ("AFPO", "MATNR"), "document_types": ("AUFK", "AUART")}, 35),
    ObjectType("MATERIAL_DOCUMENT", "Material document (goods movement)", "MM", "document",
               (L("MKPF"), L("MSEG", "MKPF", [("MBLNR", "MBLNR"), ("MJAHR", "MJAHR")])),
               ("MBLNR", "MJAHR"), ("MKPF", "BUDAT"),
               {"company_codes": ("MSEG", "BUKRS"), "plants": ("MSEG", "WERKS"), "materials": ("MSEG", "MATNR")}, 36),
]}

CONFIG_TYPES = {"COMPANY_CODE": ("T001", "BUKRS"), "PLANT": ("T001W", "WERKS")}

R, C = RelKind.REQUIRES, RelKind.CONFIG
RELATIONSHIPS: list[Relationship] = [
    Relationship("order→customer", "SALES_ORDER", "CUSTOMER", R, "VBAK", "KUNNR", description="Sold-to party"),
    Relationship("order→material", "SALES_ORDER", "MATERIAL", R, "VBAP", "MATNR"),
    Relationship("order→company code", "SALES_ORDER", "COMPANY_CODE", C, "VBAK", "BUKRS_VF"),
    Relationship("order→plant", "SALES_ORDER", "PLANT", C, "VBAP", "WERKS"),
    Relationship("delivery→order", "DELIVERY", "SALES_ORDER", R, "LIPS", "VGBEL", reverse=True,
                 description="Delivery fulfils order (document flow)"),
    Relationship("delivery→customer", "DELIVERY", "CUSTOMER", R, "LIKP", "KUNNR"),
    Relationship("delivery→material", "DELIVERY", "MATERIAL", R, "LIPS", "MATNR"),
    Relationship("delivery→plant", "DELIVERY", "PLANT", C, "LIPS", "WERKS"),
    Relationship("billing→delivery", "BILLING", "DELIVERY", R, "VBRP", "VGBEL", reverse=True),
    Relationship("billing→order", "BILLING", "SALES_ORDER", R, "VBRP", "AUBEL", reverse=True),
    Relationship("billing→customer", "BILLING", "CUSTOMER", R, "VBRK", "KUNRG"),
    Relationship("billing→material", "BILLING", "MATERIAL", R, "VBRP", "MATNR"),
    Relationship("billing→company code", "BILLING", "COMPANY_CODE", C, "VBRK", "BUKRS"),
    Relationship("fi→billing", "FI_DOCUMENT", "BILLING", R, "BKPF", "AWKEY", (("AWTYP", "VBRK"),), reverse=True,
                 description="FI document posted from billing (AWTYP=VBRK, AWKEY=VBELN)"),
    Relationship("fi→customer", "FI_DOCUMENT", "CUSTOMER", R, "BSEG", "KUNNR"),
    Relationship("fi→vendor", "FI_DOCUMENT", "VENDOR", R, "BSEG", "LIFNR"),
    Relationship("fi→company code", "FI_DOCUMENT", "COMPANY_CODE", C, "BKPF", "BUKRS"),
    Relationship("po→vendor", "PURCHASE_ORDER", "VENDOR", R, "EKKO", "LIFNR"),
    Relationship("po→material", "PURCHASE_ORDER", "MATERIAL", R, "EKPO", "MATNR"),
    Relationship("po→plant", "PURCHASE_ORDER", "PLANT", C, "EKPO", "WERKS"),
    Relationship("po→company code", "PURCHASE_ORDER", "COMPANY_CODE", C, "EKKO", "BUKRS"),
    Relationship("bom→product", "BOM", "MATERIAL", R, "STKO", "MATNR", description="The material the BOM produces"),
    Relationship("bom→component", "BOM", "MATERIAL", R, "STPO", "IDNRK"),
    Relationship("bom→plant", "BOM", "PLANT", C, "STKO", "WERKS"),
    Relationship("production order→product", "PRODUCTION_ORDER", "MATERIAL", R, "AFPO", "MATNR"),
    Relationship("production order→component", "PRODUCTION_ORDER", "MATERIAL", R, "RESB", "MATNR"),
    Relationship("production order→bom", "PRODUCTION_ORDER", "BOM", R, "AFKO", "STLNR", description="BOM the order was exploded from"),
    Relationship("production order→routing", "PRODUCTION_ORDER", "ROUTING", R, "AFKO", "PLNNR", description="Routing whose operations the order copied"),
    Relationship("routing→product", "ROUTING", "MATERIAL", R, "PLKO", "MATNR", description="The material the routing produces"),
    Relationship("routing→plant", "ROUTING", "PLANT", C, "PLKO", "WERKS"),
    Relationship("production order→plant", "PRODUCTION_ORDER", "PLANT", C, "AUFK", "WERKS"),
    Relationship("production order→company code", "PRODUCTION_ORDER", "COMPANY_CODE", C, "AUFK", "BUKRS"),
    Relationship("flight→airline", "FLIGHT", "CARRIER", R, "SFLIGHT", "CARRID", description="The airline (and its schedule) the flight belongs to"),
    Relationship("booking→customer", "FLIGHT", "TRAVEL_CUSTOMER", R, "SBOOK", "CUSTOMID"),
    Relationship("inspection lot→material", "INSPECTION_LOT", "MATERIAL", R, "QALS", "MATNR"),
    Relationship("inspection lot→production order", "INSPECTION_LOT", "PRODUCTION_ORDER", R, "QALS", "AUFNR", reverse=True,
                 description="Lot created for a production order (goods receipt inspection); a lot without an order has no such requirement"),
    Relationship("inspection lot→plant", "INSPECTION_LOT", "PLANT", C, "QALS", "WERKS"),
    Relationship("functional location→superior", "FUNC_LOCATION", "FUNC_LOCATION", R, "IFLOT", "TPLMA",
                 description="A location below another one needs its parent (a hierarchy, never a cycle)"),
    Relationship("functional location→plant", "FUNC_LOCATION", "PLANT", C, "IFLOT", "SWERK"),
    Relationship("equipment→functional location", "EQUIPMENT", "FUNC_LOCATION", R, "EQUI", "TPLNR"),
    Relationship("equipment→material", "EQUIPMENT", "MATERIAL", R, "EQUI", "MATNR", description="Only equipment that was made from a material has one"),
    Relationship("equipment→plant", "EQUIPMENT", "PLANT", C, "EQUI", "SWERK"),
    Relationship("notification→equipment", "MAINT_NOTIFICATION", "EQUIPMENT", R, "QMEL", "EQUNR"),
    Relationship("notification→functional location", "MAINT_NOTIFICATION", "FUNC_LOCATION", R, "QMEL", "TPLNR"),
    Relationship("notification→plant", "MAINT_NOTIFICATION", "PLANT", C, "QMEL", "SWERK"),
    Relationship("employee→company code", "EMPLOYEE", "COMPANY_CODE", C, "PA0001", "BUKRS"),
    Relationship("employee→plant", "EMPLOYEE", "PLANT", C, "PA0001", "WERKS"),
    Relationship("goods movement→material", "MATERIAL_DOCUMENT", "MATERIAL", R, "MSEG", "MATNR"),
    Relationship("goods movement→production order", "MATERIAL_DOCUMENT", "PRODUCTION_ORDER", R, "MSEG", "AUFNR", reverse=True,
                 description="Goods issue (261) / receipt (101) posted against a production order"),
    Relationship("goods movement→plant", "MATERIAL_DOCUMENT", "PLANT", C, "MSEG", "WERKS"),
    Relationship("goods movement→company code", "MATERIAL_DOCUMENT", "COMPANY_CODE", C, "MSEG", "BUKRS"),
    Relationship("customer→company code", "CUSTOMER", "COMPANY_CODE", C, "KNB1", "BUKRS"),
    Relationship("vendor→company code", "VENDOR", "COMPANY_CODE", C, "LFB1", "BUKRS"),
    Relationship("material→plant", "MATERIAL", "PLANT", C, "MARC", "WERKS"),
]

# Load order of types (masters first); ties inside the instance graph are resolved by dependencies.


def find_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCC: returns strongly connected components with >1 node or a self loop."""
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = [0]

    def strong(v):
        index[v] = low[v] = counter[0]; counter[0] += 1
        stack.append(v); on.add(v)
        for w in edges.get(v, ()):
            if w not in index:
                strong(w); low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop(); on.discard(w); comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or v in edges.get(v, ()):
                out.append(sorted(comp))

    import sys
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    for v in list(edges):
        if v not in index:
            strong(v)
    return out


class Registry:
    """Mutable registry; the module-level default is copied per app instance."""

    def __init__(self, family: str = "ECC"):
        self.family = family
        self.types = dict(OBJECT_TYPES)
        self.relationships = list(RELATIONSHIPS)
        if family == "S4":
            # S/4HANA: customers/vendors are Business Partners (CVI keeps KNA1/LFA1 in sync) and FI documents
            # also live in the universal journal ACDOCA.
            def add(name, link):
                self.types[name] = replace(self.types[name], tables=self.types[name].tables + (link,))
            add("CUSTOMER", L("BUT000", "KNA1", [("PARTNER", "KUNNR")]))
            add("VENDOR", L("BUT000", "LFA1", [("PARTNER", "LIFNR")]))
            add("FI_DOCUMENT", L("ACDOCA", "BKPF", [("RBUKRS", "BUKRS"), ("BELNR", "BELNR"), ("GJAHR", "GJAHR")]))
            # S/4HANA stores material documents in ONE table, MATDOC (MKPF/MSEG are compatibility views without rows). A document line is
            # therefore the unit this model loads: lines of one document are separate instances (a simplification: scoping by material
            # can leave a document partial).
            self.types["MATERIAL_DOCUMENT"] = ObjectType(
                "MATERIAL_DOCUMENT", "Material document line (S/4HANA MATDOC)", "MM", "document", (L("MATDOC"),), ("MBLNR", "MJAHR", "ZEILE"), ("MATDOC", "BUDAT"),
                {"company_codes": ("MATDOC", "BUKRS"), "plants": ("MATDOC", "WERKS"), "materials": ("MATDOC", "MATNR")}, 36)
            self.relationships = [replace(r, via_table="MATDOC") if r.via_table == "MSEG" else r for r in self.relationships]

    def register_object_type(self, ot: ObjectType) -> None:
        self.types[ot.name] = ot

    def register_relationship(self, rel: Relationship) -> None:
        self.relationships.append(rel)

    def type_edges(self) -> dict[str, set[str]]:
        e: dict[str, set[str]] = {t: set() for t in self.types}
        for r in self.relationships:
            if r.kind == RelKind.REQUIRES and r.src != r.dst:  # a type that refers to itself is a hierarchy; a loop among its instances is still found by the planner
                e.setdefault(r.src, set()).add(r.dst)
        return e

    def validate(self) -> dict:
        problems = []
        for r in self.relationships:
            if r.src not in self.types:
                problems.append(f"{r.name}: unknown source type {r.src}")
            if r.kind == RelKind.REQUIRES and r.dst not in self.types:
                problems.append(f"{r.name}: unknown target type {r.dst}")
            if r.kind == RelKind.CONFIG and r.dst not in CONFIG_TYPES:
                problems.append(f"{r.name}: unknown config type {r.dst}")
        cycles = find_cycles(self.type_edges())
        return {"ok": not problems and not cycles, "problems": problems, "type_cycles": cycles}

    def graph(self) -> dict:
        return {
            "nodes": [{"id": t.name, "label": t.label, "module": t.module, "kind": t.kind,
                       "tables": [l.table for l in t.tables]} for t in self.types.values()]
                     + [{"id": c, "label": c.replace("_", " ").title(), "module": "CONFIG", "kind": "config",
                         "tables": [CONFIG_TYPES[c][0]]} for c in CONFIG_TYPES],
            "edges": [{"from": r.src, "to": r.dst, "kind": r.kind.value, "name": r.name,
                       "via": f"{r.via_table}.{r.via_field}", "downstream": r.reverse} for r in self.relationships],
            "validation": self.validate(),
        }
