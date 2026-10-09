# Verifying the API bindings against `$metadata`

The entity bindings (`backend/sdtf/catalog/api_bindings.py`), the journal item mapping and the read-only
bindings were written from the public API reference. They are verified in two steps: against what can be read
publicly now, and against the target's own `$metadata` on your machine (A4H), with a check that runs the same
way in both cases.

## The check

* `sdtf metadata check --file <service>.xml --service <SERVICE>` checks one downloaded EDMX document (from the
  target: `https://<host>:44300/sap/opu/odata/sap/<SERVICE>/$metadata`, or from the SAP API hub's "API
  Specification" download); `POST /metadata/check` does the same over the API.
* `sdtf metadata check --system <target id>` (`POST /systems/{id}/connector/metadata-check`, Landscape page)
  reads the gateway catalogue (which services are activated) and the `$metadata` of every bound service from
  a registered API target, reads only.
* `sdtf metadata expectations` (`GET /metadata/expectations`) lists what the platform relies on per service:
  entity sets, properties, keys.

For every service the report says VERIFIED, DEVIATIONS (properties missing, keys differ: the closest names
the service has are listed, so a rename is one look away), ENTITY_SETS_MISSING or UNAVAILABLE (not activated,
or no reachable `$metadata`). Nothing is changed automatically: correct the binding, re-run.

On the simulated gateway every service is VERIFIED by construction (its `$metadata` is generated from the
bindings); the check only means something against a real target or a downloaded specification.

## What the public references confirmed on 2026-10-09 (without system access)

The SAP API hub itself is not reachable from the build environment; these come from the SAP Cloud SDK
reference pages, SAP notes and community posts found by search, which quote the OData property names.

| Service / entity | Confirmed | Not confirmed (metadata check will tell) | Consequence in the platform |
|---|---|---|---|
| `API_JOURNALENTRYITEMBASIC_SRV` / `A_JournalEntryItemBasic` | the service and collection, filter by `CompanyCode` and `FiscalYear`; `AmountInCompanyCodeCurrency` (Edm.Decimal); `LedgerGLLineItem` as line key (CDS `I_JournalEntryItem`); `DebitCreditCode` (CDS view; the SDK class shows `ControllingDebitCreditCode` too); sign convention: a positive amount with `H` or a negative amount with `S` is a negative posting | `FinancialAccountType`, `ClearingAccountingDocument`, `ClearingDate`, `SpecialGLCode`, `AssignmentReference` (confirmed on the bank-reconciliation `JournalEntryItem`, not on the basic entity), `DocumentReferenceID`, `OriginalReferenceDocument*`, `AccountingDocumentItem`, `PartnerCompany` | the line derivation uses `DebitCreditCode` with the absolute amount (matches the convention), falls back to the amount's sign without it; open items (`FinancialAccountType` D/K, no clearing document) and the idempotency lookup by `DocumentReferenceID` depend on unconfirmed properties: the check marks them optional and lists what is there |
| `API_PRODUCT_SRV` / `A_ProductValuation` | keys `Product`, `ValuationArea`, `ValuationType`; `ValuationClass`; `PriceDeterminationControl` (1 char; the binding said `PriceControl` and was corrected); `StandardPrice`, `MovingAveragePrice`; `PriceUnitQty`, `InventoryValuationProcedure`, `ValuationCategory`; prices from ledger 0L only (SAP note 3071781) | `ValuationQuantity`, `TotalValue`, `Currency` (the product API exposes prices; the stock value may not be there) | when the entity carries no `TotalValue`, the target view reports material valuation as not readable and the inventory check says so (WARN) instead of comparing against zero |
| `API_FIXEDASSET` / `FixedAssetValuation` (read-only binding for asset values) | nothing: no public reference for a released **on-premise OData read service of asset values** was found. The public cloud lists `API_FIXEDASSET_G4BA` (OData V4, master data) and the CDS view `I_AssetValuationForLedger` for values; on-premise lists posting services (`API_FIXEDASSETACQUISITION_G4BA`, revaluation, usage object) | the whole binding | placeholder: the check will report the entity set missing; on a target registered with the read-only add-on as well (`meta.rfc`) the asset values are then read over RFC: on S/4HANA from the compatibility view `FAAV_ANLC`, else the APC line items of `ACDOCA` / `FAAT_DOC_IT` by movement category, else the net postings (declared as a different measure); on ECC-type systems from ANLC; otherwise the asset check stays "not readable" (WARN) |
| the other load bindings (`A_BusinessPartner`, `A_Supplier`, `A_Product`, `A_SalesOrder`, `A_PurchaseOrder`, `A_OutbDeliveryHeader`, `A_CostCenter`, `A_ProfitCenter`, `A_ProductionOrder_2`) | service and entity set names (public API reference) | property names beyond the keys, the address flattening on the business partner, extension properties | the check lists every missing property with suggestions; `docs/02-sap-connectivity-design.md` lists what to verify |

Sources used (reachable through search): [JournalEntryItemBasic (SAP Cloud SDK)](https://help.sap.com/doc/1fd0ac329d664076acfd210249536594/1.0/en-US/classes/_sap_cloud_sdk_vdm_journal_entry_item_basic_service.journalentryitembasic.html),
[I_JournalEntryItem fields](https://queryviz.io/cds-views/I_JournalEntryItem), [Journal Entry API (sign convention)](https://help.sap.com/docs/SAP_S4HANA_CLOUD/b978f98fc5884ff2aeb10c8fdeb8a43b/92fed0579212c525e10000000a4450e5.html),
[ProductValuation (SAP Cloud SDK)](https://help.sap.com/doc/6599292dcb7243bc95071d9d1757a30f/1.0/en-US/com/sap/cloud/sdk/s4hana/datamodel/odata/namespaces/productmaster/ProductValuation.html), [SAP note 3071781](https://userapps.support.sap.com/sap/support/knowledge/en/3071781), [SAP note 3125002](https://userapps.support.sap.com/sap/support/knowledge/en/3125002),
[Fixed Asset APIs (on-premise)](https://help.sap.com/docs/SAP_S4HANA_ON-PREMISE/3ce94f5ed9674681a75bd84dd8d2b207/0bb55e6d8c6a4353afc20d3542cbc508.html), [Asset Accounting configuration guide (public cloud)](https://community.sap.com/t5/technology-blog-posts-by-sap/asset-accounting-configuration-guide-for-sap-s-4hana-public-cloud-edition/ba-p/14134075).

## On A4H

0. No registration needed for the first run. On the PC that reaches the VM (A4H up, the platform installed:
   `cd backend && pip install -e ".[dev]"`):

   ```powershell
   $env:A4H_PW = "<password of DEVELOPER>"
   sdtf metadata check --url https://vhcala4hci.dummy.nodomain:44300 --user DEVELOPER --passwd-env A4H_PW --no-verify --out a4h-metadata.md
   ```
   (`--no-verify` only because the appliance ships a self-signed certificate; `--services A,B` limits the
   check; the password is read from the environment, never from the command line.) The gateway catalogue
   (`/sap/opu/odata/iwfnd/catalogservice;v=2`) tells which services are activated; a service that is not
   activated shows as UNAVAILABLE and is activated in `/IWFND/MAINT_SERVICE`.
1. Later, with A4H registered as an API target (`docs/connect-real-systems.md`, step 3), the same check runs as
   **Metadata check** on the Landscape page or `sdtf metadata check --system <id> --out a4h-metadata.md`.
2. Send back `a4h-metadata.md`. Every DEVIATIONS row names the property the platform expects and the closest
   names the service has; the correction is a one-line change in `catalog/api_bindings.py` or
   `reconciliation/views.py` (`JOURNAL_FIELDS`), after which the same check passes.
3. Until then, the reconciliation stays honest by construction: a missing entity set or property makes the
   affected table *not readable* (WARN with the reason), never a false PASS or FAIL.
