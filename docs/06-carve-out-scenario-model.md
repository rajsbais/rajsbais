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

## Ownership safeguards
* ParentCo data is never loaded into SpinCo: partial documents are split at the company-code boundary during
  extraction, verified by the `organizational_assignment` reconciliation check.
* SpinCo data retained in the source is listed in the residual exposure report with the disposition required.
* Financial integrity: trial balance and GL balance checks per company code, intercompany balance comparison with
  explanations for counterpart documents that stayed with the seller.
