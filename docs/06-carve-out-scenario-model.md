# 06 — Carve-out scenario model

Implemented in `scope/models.py` (definition), `scope/service.py` (classification, dispositions, approval) and
`carveout/service.py` (reports). The reference scenario is a **forward carve-out of company code 5000 (Nordlicht
Specialty Materials GmbH) from an ECC 6.0 EHP8 group into an independent S/4HANA 2025 SpinCo** (demo project).

## Scope definition (what the business decides)
| Field | Options | Effect |
|---|---|---|
| `company_codes`, `plants` | org filters | seeds |
| `fiscal_year_from/to`, `document_status` | ALL / OPEN_ONLY / CLOSED_ONLY | time & status selection |
| `historical_policy` | FULL / OPEN_ITEMS_AND_BALANCES / YEARS | closed history retained by seller or moved |
| `shared_object_policy` | DUPLICATE / REFERENCE / EXCLUDE / MANUAL | shared customers, vendors, materials, controlling area |
| `cross_company_policy` | INCLUDE_FLAG / REFERENCE / EXCLUDE | documents touching ParentCo and SpinCo |
| `edge_policies`, `type_policies` | FOLLOW / REFERENCE / STOP / FLAG | traversal overrides |
| `target_ownership` | company code / plant / controlling area maps | ParentCo→SpinCo org mapping |
| `carve_out_direction` | FORWARD / REVERSE | reverse = ParentCo data extracted out of a system that SpinCo keeps |

## Classification (what the engine decides, and explains)
| Class | Rule |
|---|---|
| FULLY_TRANSFERRED | owned exclusively by SpinCo company codes (or client-level master reached via FULL inclusion) |
| PARTIALLY_TRANSFERRED | transactional document owned by SpinCo that touches ParentCo (cross-company sale/PO/stock transfer/posting); only the SpinCo side moves; requires approval |
| SHARED_DUPLICATED | master data with views in both; general data + SpinCo views duplicated |
| RETAINED_BY_SELLER | ParentCo-owned counterparts, or SpinCo closed history under OPEN_ITEMS_AND_BALANCES |
| REFERENCE_ONLY | needed for referential integrity but not owned (loaded as general-data stubs for masters, not at all for documents) |
| EXCLUDED | excluded by type or policy |
| MANUAL_DISPOSITION | technical objects, export-controlled materials, MANUAL shared policy, traversal FLAG |

Dispositions (`TRANSFER`, `RETAIN`, `DUPLICATE`, `REFERENCE`, `EXCLUDE`) by an approver re-classify DRAFT manifests
and are recorded on the node; a manifest cannot be approved while any object still requires approval.

## Detections and reports
Cross-company postings · shared customers/vendors/materials · cross-company sales and POs · cross-plant stock transfers
· open sales/purchase/accounting documents · shared controlling structures · export-controlled records ·
intercompany balances per counterpart · TSA services · residual cleanup candidates (never executed automatically).

## Deal templates
`carveout/deals.py`, `GET /carveout/deal-templates`, `POST /projects/{id}/manifests/from-deal`, `GET /manifests/{id}/carveout/deal`,
`sdtf deal-templates`. A template fills the policies a business has not set and names what the deal implies; a
policy set explicitly is kept and listed as a deviation with its consequence, never refused.

| Template | Legal entity | Policies | Residual rule | Approvals |
|---|---|---|---|---|
| ASSET_DEAL | stays with the seller | OPEN_ITEMS_AND_BALANCES, OPEN_ONLY, DUPLICATE, REFERENCE | RETAIN_AS_LEGAL_RECORD: the seller keeps every record; the cleanup plan deletes nothing | BUSINESS, LEGAL |
| SHARE_DEAL | transfers | FULL, ALL, DUPLICATE, INCLUDE_FLAG | CLEANUP_AFTER_TSA: the entity's data leaves the seller after the TSA | BUSINESS, LEGAL, FINANCE |
| HIVE_DOWN | new entity in the target, then sold | as SHARE_DEAL, company codes renumbered to the new one | CLEANUP_AFTER_TSA | BUSINESS, LEGAL, FINANCE |

The templates are the platform's policies for which history moves, what stays behind and who signs; they are not
legal advice. `deal_type` on the scope definition records the template; the residual exposure report carries the
deal's residual rule and obligations.

## Residual cleanup plans
`carveout/cleanup.py`, `POST /manifests/{id}/carveout/cleanup-plans` and `/carveout/cleanup-plans/{id}/...`,
`sdtf residual-cleanup`, Carve-out studio (Residual exposure tab).

A plan takes every cleanup candidate of the manifest (company-code views of shared customers, vendors and
materials that still show the carved-out company codes) as an item with its action: DELETE_VIEW_AFTER_APPROVAL,
ARCHIVE_AFTER_APPROVAL, or under an asset deal FLAG_TRANSFERRED_KEEP for every item (nothing would change the
source). Items are included by default and excluded with a note while the plan is a draft. **Approval** is a
business approval under four eyes (not the plan's creator) and is refused until the manifest is approved and a
completed run on it has reconciled PASS or WARN: the source is cleaned only after the data has demonstrably
arrived. **Export** writes the work package: one CSV per table with the affected rows (full payload) and the
action, a JSON index with hashes, zipped under the evidence directory. **Execution** is possible on the
platform's simulated source only: the package is written first (the archive copy), then the record-store rows of
the included DELETE and ARCHIVE items are removed and every item carries its result; the next residual report and
the next plan show what is left. For a source reached through the read-only add-on the execution is refused with
the reason and the exported package is the deliverable for the SAP-side archiving / deletion run. Every step is an
audit event; the Markdown report lists items, decisions, results and the package files.

## Ownership safeguards
* ParentCo data is never loaded into SpinCo: partial documents are split at the company-code boundary during
  extraction, verified by the `organizational_assignment` reconciliation check.
* SpinCo data retained in the source is listed in the residual exposure report with the disposition required.
* Financial integrity: trial balance and GL balance checks per company code, intercompany balance comparison with
  explanations for counterpart documents that stayed with the seller.
