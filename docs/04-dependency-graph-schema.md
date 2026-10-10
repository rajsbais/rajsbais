# 04 — Dependency graph schema

Persisted in `graph_nodes` / `graph_edges` (relational, ADR-0004); built by `graph/service.py::build_graph`.

## Nodes
`node_id = "<object_type>:<key>"` · `node_type` · `label` · `attributes`: `company_codes[]`, `kind`, `domain`,
`cross_company` (transactional object touching >1 company code), `shared` (master data with views in >1 company
code), `missing` (dangling reference target). Systems, org units and documents are all nodes; tables are described
in the catalog and referenced through object types.

## Edge types
| Type | Example | Resolver |
|---|---|---|
| DOC_FLOW | SalesOrder→Delivery→Billing; PO→GR→IR; ProductionOrder→Consumption; AccountingDocument→Clearing | VBFA, EKBE, MSEG.AUFNR, BSEG.AUGBL |
| ACCOUNTING_REF | Billing/MaterialDocument/InvoiceReceipt→AccountingDocument | BKPF.AWTYP/AWKEY |
| MASTER_REF | documents→Customer/Vendor/Material/GLAccount/CostCenter | document fields |
| ORG_OWNERSHIP | Customer→CompanyCode (KNB1), Material→Plant (MARC), Plant→CompanyCode | views / assignment tables |
| PARENT_CHILD | Material→ExportControl, Vendor→SupplierExtension | custom tables |
| CROSS_COMPANY | AccountingDocument↔counterpart (BVORG) | BKPF.BVORG |
| SHARED | (reserved; shared status is a node attribute today) | — |

## Traversal
`traverse(graph, seeds, TraversalPolicy)` — BFS with per-edge-type and per-target-type policies
FOLLOW / REFERENCE / STOP / FLAG, max depth, direction. Output: `included{node: FULL|REFERENCE|FLAGGED}`, `traces[]`
(node, from, edge, policy, depth, reason), `stopped[]`, `missing[]`. Inclusion strength only ever increases
(REFERENCE < FLAGGED < FULL) so results are order-independent.

## Server-side traversal
`GraphStore.traverse` is the entry point used by scope evaluation and the API. The relational store runs the
algorithm above in process; the Neo4j store runs it as a frontier traversal: per depth level one adjacency query
over the current frontier (chunks of 5,000 ids, reverse query added for `direction=BOTH`), with policy decisions
made by the shared `apply_edge_policy` function, so results are identical and memory is bounded by the included
set rather than the graph. Scope evaluation streams node rows only (`iter_nodes`) for seeding and classification.

## Version awareness
Node types and resolvers are keyed by object type; the catalog carries ECC vs S/4 applicability and the compatibility
registry maps data-model changes (MKPF/MSEG→MATDOC, BSEG→ACDOCA, KNA1/LFA1→BP). A second resolver set for S/4 sources
(e.g. document flow via I_SalesDocumentFlow CDS) plugs into `RELATIONSHIPS` without changing traversal.
