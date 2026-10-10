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

## Implemented object types (30)
CFG: CompanyCode, Plant, SalesOrg, PurchOrg, ControllingArea · FI: GLAccount, FixedAsset, AccountingDocument ·
CO: CostCenter, ProfitCenter · MD: Customer, Vendor, Material, BillOfMaterial, Routing, WorkCenter, Batch ·
SD: SalesOrder, Delivery, BillingDocument · MM: PurchaseOrder, MaterialDocument, InvoiceReceipt · PP: ProductionOrder ·
BASIS: RfcDestination, IdocPartner, BackgroundJob · Z: ExportControl, TsaScope, SupplierExt.

The manufacturing masters keep SAP's own identities and are plant-scoped through their assignments: a bill of
material is the BOM header and alternative (STKO, key STLNR / STLAL) with its items (STPO) and its material-plant-usage
assignments (MAST); a routing is the task list header and group counter (PLKO, key PLNNR / PLNTY / PLNAL) with its
operations (PLPO) and its material-plant assignments (MAPL); a work center (CRHD) carries its cost center assignment
(CRCO); a batch is the material's batch (MCH1, key CHARG / MATNR) with its plant batches (MCHA) and batch stock
(MCHB). The object key is a prefix of every item key, which is what the cockpit export and the delta loaders rely on
to group rows into instances. The company codes come from the plants of the assignments through the valuation area
(T001K), so a carve-out of a company code takes the masters assigned in its plants, only those plant views move
(like MARC / MBEW / MARD for materials) and the plant map renames them. In a merger the non-leading sources' BOM,
task list and work center numbers move into the disjoint number range with the documents and batch numbers get a
source prefix. The reduced table model folds STAS and PLAS into the items (the alternative is carried on the item /
operation) and the work center text into the header.

## Business object migration packages (section I) — status
| Domain | Objects | Package status |
|---|---|---|
| Finance | GL accounts, accounting documents (incl. open items/clearing), fixed assets, cost/profit centers | SIMULATED end to end incl. reconciliation |
| Sales | customers (BP), sales orders, deliveries, billing | SIMULATED |
| Procurement/Inventory | vendors (BP), materials incl. plant/valuation/stock views, POs, goods movements, invoices | SIMULATED |
| Manufacturing | production orders, component consumption, confirmations, settlement cost center; bills of material, routings, work centers, batches as plant-scoped masters | SIMULATED (serial numbers, scheduling agreements, contracts PLANNED) |
| QM, PM, PS, WM/EWM, TM, MDG, industry | — | PLANNED |
Each package = object type + relationships + load methods + reconciliation checks + tests (`tests/`). Raw table copy
is never treated as object migration: objects are extracted by instance, split at company-code boundaries, and
validated by chain/financial checks.
