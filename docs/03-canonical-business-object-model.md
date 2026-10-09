# 03 — Canonical SAP business object model

Source of truth: `backend/sdtf/catalog/business_objects.py` (`BUSINESS_OBJECTS`, `RELATIONSHIPS`) and
`backend/sdtf/catalog/tables.py` (DDIC subset). The API exposes both at `/catalog/business-objects` and `/catalog/tables`.

## Object type definition
| Attribute | Meaning |
|---|---|
| `id` | `<DOMAIN>.<Name>` e.g. `FI.AccountingDocument` |
| `kind` | CONFIG · MASTER · TRANSACTIONAL · TECHNICAL (drives classification rules) |
| `header_table`, `item_tables`, `key_fields` | leading table and dependent rows extracted with the object |
| `org_scope`, `org_field` | COMPANY_CODE / PLANT / CLIENT / CONTROLLING_AREA ownership |
| `year_field` | fiscal-year attribute used by time-based selection |
| `applicability` | ECC / S4HANA |
| `load_methods` | per target product: method, API/object, note |
| `s4_simplification` | data-model change relevant to ECC→S/4 |

Instance derivation (`instance_company_codes`, `instance_status`) is *explicit per type* — e.g. a sales order's
company codes are its selling company code **plus** the company codes of its delivering plants; an accounting
document's company codes include all BVORG counterparts. This is how cross-company and shared objects are detected
without relying on DDIC foreign keys.

## Implemented object types (26)
CFG: CompanyCode, Plant, SalesOrg, PurchOrg, ControllingArea · FI: GLAccount, FixedAsset, AccountingDocument ·
CO: CostCenter, ProfitCenter · MD: Customer, Vendor, Material · SD: SalesOrder, Delivery, BillingDocument ·
MM: PurchaseOrder, MaterialDocument, InvoiceReceipt · PP: ProductionOrder · BASIS: RfcDestination, IdocPartner,
BackgroundJob · Z: ExportControl, TsaScope, SupplierExt.

## Business object migration packages (section I) — status
| Domain | Objects | Package status |
|---|---|---|
| Finance | GL accounts, accounting documents (incl. open items/clearing), fixed assets, cost/profit centers | SIMULATED end to end incl. reconciliation |
| Sales | customers (BP), sales orders, deliveries, billing | SIMULATED |
| Procurement/Inventory | vendors (BP), materials incl. plant/valuation/stock views, POs, goods movements, invoices | SIMULATED |
| Manufacturing | production orders, component consumption, confirmations, settlement cost center | SIMULATED (BOM/routing/work center PLANNED) |
| QM, PM, PS, WM/EWM, TM, MDG, industry | — | PLANNED |
Each package = object type + relationships + load methods + reconciliation checks + tests (`tests/`). Raw table copy
is never treated as object migration: objects are extracted by instance, split at company-code boundaries, and
validated by chain/financial checks.
