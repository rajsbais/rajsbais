"""Bindings between SAP-like tables and the released S/4HANA APIs the platform loads through (ADR-0007/0015).

A binding says, per business object: the API service, the entity set for the header and each item table, which
API property each table field maps to, which properties the target derives (read-only), which may be changed on an
existing entity, how a deletion is expressed (DELETE, reversal posting, block flag) and who assigns numbers.
Unmapped table fields travel as custom-field extension properties (`YY1_<FIELD>`), which real targets must expose
through key-user extensibility; their share is reported per call so nobody mistakes a partial mapping for a full one.

These are first mappings written from the public API reference, not validated against a target's `$metadata`;
`docs/02-sap-connectivity-design.md` lists what to verify on a real system.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .tables import TABLES

EXT_PREFIX = "YY1_"


@dataclass(frozen=True)
class EntityBinding:
    table: str
    entity_set: str
    fields: dict[str, str]  # table field -> API property
    key_props: tuple[str, ...]  # API key properties, in table key order
    derived: tuple[str, ...] = ()  # table fields the target computes (never sent; compared after load)
    updatable: tuple[str, ...] = ()  # table fields that may change on an existing entity
    parent_props: tuple[str, ...] = ()  # properties that carry the header key (items)
    priced: tuple[str, ...] = ()  # sent on create (manual condition), recomputed by the target afterwards (never patched)

    def to_entity(self, row: dict, create: bool = True) -> dict:
        out: dict = {}
        for f, v in row.items():
            if f in self.derived or (f in self.priced and not create):
                continue
            out[self.fields.get(f) or f"{EXT_PREFIX}{f}"] = v
        return out

    def to_row(self, entity: dict) -> dict:
        inv = {p: f for f, p in self.fields.items()}
        row: dict = {}
        for p, v in entity.items():
            if p.startswith("to_") or p.startswith("__"):
                continue
            if p in inv:
                row[inv[p]] = v
            elif p.startswith(EXT_PREFIX):
                row[p[len(EXT_PREFIX) :]] = v
        return row

    def coverage(self) -> tuple[int, int]:
        td = TABLES.get(self.table)
        total = len(td.fields) if td else len(self.fields)
        return len([f for f in (td.fields if td else self.fields) if f in self.fields or f in self.derived or f in self.priced]), total

    def key_of_entity(self, entity: dict) -> str:
        return "|".join(str(entity.get(p, "")) for p in self.key_props)


@dataclass(frozen=True)
class ApiBinding:
    object_type: str
    service: str
    protocol: str  # ODATA_V2 | SOAP
    header: EntityBinding
    items: dict[str, EntityBinding] = field(default_factory=dict)
    numbering: str = "external"  # external: the transformed key is proposed; internal: the target assigns
    on_delete: str = "DELETE"  # DELETE | REVERSAL | BLOCK | FORBIDDEN
    deep_insert: bool = True  # header + items in one create
    history_rule: str = ""  # explanation when an instance is history and must not be re-posted
    history_when: Callable[[dict, list[dict]], bool] | None = None  # (header row, item rows) -> is this instance history?
    notes: str = ""

    def entity_for(self, table: str) -> EntityBinding | None:
        if table == self.header.table:
            return self.header
        return self.items.get(table)


def _e(table, entity_set, fields, key_props, derived=(), updatable=(), parent_props=(), priced=()):
    return EntityBinding(table, entity_set, fields, tuple(key_props), tuple(derived), tuple(updatable), tuple(parent_props), tuple(priced))


API_BINDINGS: dict[str, ApiBinding] = {
    b.object_type: b
    for b in [
        ApiBinding(
            "MD.Customer", "API_BUSINESS_PARTNER", "ODATA_V2",
            _e("KNA1", "A_BusinessPartner", {"KUNNR": "BusinessPartner", "NAME1": "OrganizationBPName1", "LAND1": "Country", "VBUND": "TradingPartner", "KTOKD": "BusinessPartnerGrouping"}, ("BusinessPartner",), updatable=("NAME1", "LAND1", "VBUND")),
            {"KNB1": _e("KNB1", "A_CustomerCompany", {"KUNNR": "Customer", "BUKRS": "CompanyCode", "AKONT": "ReconciliationAccount", "ZTERM": "PaymentTerms"}, ("Customer", "CompanyCode"), updatable=("AKONT", "ZTERM"), parent_props=("Customer",))},
            on_delete="BLOCK", deep_insert=False,
            notes="Country is an address property in the real service (to_BusinessPartnerAddress); flattened here, refine against $metadata",
        ),
        ApiBinding(
            "MD.Vendor", "API_BUSINESS_PARTNER", "ODATA_V2",
            _e("LFA1", "A_Supplier", {"LIFNR": "Supplier", "NAME1": "SupplierName", "LAND1": "Country", "VBUND": "TradingPartner", "KTOKK": "SupplierAccountGroup"}, ("Supplier",), updatable=("NAME1", "LAND1", "VBUND")),
            {"LFB1": _e("LFB1", "A_SupplierCompany", {"LIFNR": "Supplier", "BUKRS": "CompanyCode", "AKONT": "ReconciliationAccount", "ZTERM": "PaymentTerms"}, ("Supplier", "CompanyCode"), updatable=("AKONT", "ZTERM"), parent_props=("Supplier",))},
            on_delete="BLOCK", deep_insert=False,
        ),
        ApiBinding(
            "MD.Material", "API_PRODUCT_SRV", "ODATA_V2",
            _e("MARA", "A_Product", {"MATNR": "Product", "MTART": "ProductType", "MATKL": "ProductGroup", "MEINS": "BaseUnit", "MAKTX": "ProductDescription"}, ("Product",), updatable=("MATKL", "MAKTX")),
            {
                "MARC": _e("MARC", "A_ProductPlant", {"MATNR": "Product", "WERKS": "Plant", "DISPO": "MRPResponsible", "EKGRP": "PurchasingGroup", "BESKZ": "ProcurementType"}, ("Product", "Plant"), updatable=("DISPO", "EKGRP", "BESKZ"), parent_props=("Product",)),
                "MBEW": _e("MBEW", "A_ProductValuation", {"MATNR": "Product", "BWKEY": "ValuationArea", "BWTAR": "ValuationType", "VPRSV": "PriceDeterminationControl", "VERPR": "MovingAveragePrice", "STPRS": "StandardPrice", "LBKUM": "ValuationQuantity", "SALK3": "TotalValue", "WAERS": "Currency"}, ("Product", "ValuationArea", "ValuationType"), updatable=("VPRSV", "VERPR", "STPRS"), parent_props=("Product",)),  # PriceDeterminationControl, StandardPrice, MovingAveragePrice, ValuationClass confirmed by the public SDK reference; ValuationQuantity / TotalValue / Currency are NOT confirmed (the product API exposes prices, the stock value may not be there): the metadata check reports it, the inventory check then says "not readable"
                "MARD": _e("MARD", "A_ProductStorageLocation", {"MATNR": "Product", "WERKS": "Plant", "LGORT": "StorageLocation"}, ("Product", "Plant", "StorageLocation"), parent_props=("Product",)),
            },
            on_delete="BLOCK", deep_insert=False,
            notes="Stock quantities (MARD) are never loaded through the product API; they come from goods movements / the migration cockpit",
        ),
        ApiBinding(
            "SD.SalesOrder", "API_SALES_ORDER_SRV", "ODATA_V2",
            _e("VBAK", "A_SalesOrder", {"VBELN": "SalesOrder", "AUART": "SalesOrderType", "VKORG": "SalesOrganization", "VTWEG": "DistributionChannel", "SPART": "OrganizationDivision", "KUNNR": "SoldToParty", "AUDAT": "SalesOrderDate", "WAERK": "TransactionCurrency", "NETWR": "TotalNetAmount", "GBSTK": "OverallSDProcessStatus", "BUKRS_VF": "BillingCompanyCode"}, ("SalesOrder",), derived=("NETWR", "GBSTK", "BUKRS_VF"), updatable=("AUDAT", "KUNNR")),
            {"VBAP": _e("VBAP", "A_SalesOrderItem", {"VBELN": "SalesOrder", "POSNR": "SalesOrderItem", "MATNR": "Material", "WERKS": "ProductionPlant", "KWMENG": "RequestedQuantity", "NETWR": "NetAmount", "PRCTR": "ProfitCenter"}, ("SalesOrder", "SalesOrderItem"), updatable=("KWMENG", "WERKS", "PRCTR"), parent_props=("SalesOrder",), priced=("NETWR",))},
            on_delete="DELETE",
            history_rule="completed sales orders are history: they are not re-created through the API (their status cannot be set), they migrate as history",
            history_when=lambda h, items: h.get("GBSTK") == "C",
            notes="TotalNetAmount/NetAmount are priced by the target (condition kept from the item's unit price); status and billing company code are derived",
        ),
        ApiBinding(
            "MM.PurchaseOrder", "API_PURCHASEORDER_PROCESS_SRV", "ODATA_V2",
            _e("EKKO", "A_PurchaseOrder", {"EBELN": "PurchaseOrder", "BUKRS": "CompanyCode", "BSTYP": "PurchasingDocumentCategory", "BSART": "PurchaseOrderType", "LIFNR": "Supplier", "EKORG": "PurchasingOrganization", "BEDAT": "PurchaseOrderDate", "WAERS": "DocumentCurrency"}, ("PurchaseOrder",), updatable=("BEDAT",)),
            {"EKPO": _e("EKPO", "A_PurchaseOrderItem", {"EBELN": "PurchaseOrder", "EBELP": "PurchaseOrderItem", "MATNR": "Material", "WERKS": "Plant", "MENGE": "OrderQuantity", "NETPR": "NetPriceAmount", "NETWR": "NetAmount", "ELIKZ": "IsCompletelyDelivered"}, ("PurchaseOrder", "PurchaseOrderItem"), updatable=("MENGE", "NETPR", "WERKS"), parent_props=("PurchaseOrder",), priced=("NETWR",))},
            on_delete="DELETE",
            history_rule="fully delivered purchase orders are history: not re-created, migrated as history",
            history_when=lambda h, items: bool(items) and all(i.get("ELIKZ") == "X" for i in items),
            notes="EKBE (PO history) is never re-posted: it follows from goods receipts and invoices in the target",
        ),
        ApiBinding(
            "SD.Delivery", "API_OUTBOUND_DELIVERY_SRV", "ODATA_V2",
            _e("LIKP", "A_OutbDeliveryHeader", {"VBELN": "DeliveryDocument", "LFART": "DeliveryDocumentType", "VSTEL": "ShippingPoint", "KUNNR": "ShipToParty", "WADAT_IST": "ActualGoodsMovementDate", "WERKS": "Plant", "BUKRS": "CompanyCode"}, ("DeliveryDocument",), derived=("BUKRS",), updatable=("VSTEL",)),
            {"LIPS": _e("LIPS", "A_OutbDeliveryItem", {"VBELN": "DeliveryDocument", "POSNR": "DeliveryDocumentItem", "MATNR": "Material", "WERKS": "Plant", "LFIMG": "ActualDeliveryQuantity", "VGBEL": "ReferenceSDDocument", "VGPOS": "ReferenceSDDocumentItem"}, ("DeliveryDocument", "DeliveryDocumentItem"), updatable=("LFIMG",), parent_props=("DeliveryDocument",))},
            on_delete="DELETE",
            history_rule="deliveries with an actual goods-movement date are history: they are not re-created, their stock and FI effects migrate as balances",
            history_when=lambda h, items: bool(h.get("WADAT_IST")),
        ),
        ApiBinding(
            "FI.AccountingDocument", "API_JOURNALENTRY_SRV", "SOAP",
            _e("BKPF", "JournalEntry", {"BUKRS": "CompanyCode", "BELNR": "AccountingDocument", "GJAHR": "FiscalYear", "BLART": "AccountingDocumentType", "BLDAT": "DocumentDate", "BUDAT": "PostingDate", "MONAT": "FiscalPeriod", "WAERS": "TransactionCurrency", "AWTYP": "OriginalReferenceDocumentType", "AWKEY": "OriginalReferenceDocument", "BVORG": "CrossCompanyTransaction", "XBLNR": "DocumentReferenceID", "BSTAT": "DocumentStatus"}, ("CompanyCode", "AccountingDocument", "FiscalYear"), derived=("BELNR", "MONAT")),
            {
                "BSEG": _e("BSEG", "JournalEntryItem", {"BUKRS": "CompanyCode", "BELNR": "AccountingDocument", "GJAHR": "FiscalYear", "BUZEI": "ItemNumber", "KOART": "AccountType", "SHKZG": "DebitCreditCode", "HKONT": "GLAccount", "DMBTR": "CompanyCodeCurrencyAmount", "WRBTR": "TransactionCurrencyAmount", "KUNNR": "Customer", "LIFNR": "Supplier", "KOSTL": "CostCenter", "PRCTR": "ProfitCenter", "AUGBL": "ClearingDocument", "AUGDT": "ClearingDate", "VBUND": "TradingPartner", "MATNR": "Material", "WERKS": "Plant"}, ("CompanyCode", "AccountingDocument", "FiscalYear", "ItemNumber"), derived=("BELNR",), parent_props=("CompanyCode", "AccountingDocument", "FiscalYear")),
                "BSID": _e("BSID", "CustomerOpenItem", {}, ("BUKRS", "KUNNR", "UMSKS", "UMSKZ", "AUGDT", "AUGBL", "ZUONR", "GJAHR", "BELNR", "BUZEI")),
                "BSIK": _e("BSIK", "SupplierOpenItem", {}, ("BUKRS", "LIFNR", "UMSKS", "UMSKZ", "AUGDT", "AUGBL", "ZUONR", "GJAHR", "BELNR", "BUZEI")),
            },
            numbering="internal", on_delete="REVERSAL", deep_insert=True,
            notes="JournalEntryBulkCreateRequestConfirmation_In: the target assigns the document number; changes are reversal + re-posting; open items (BSID/BSIK) are derived from customer/supplier lines",
        ),
        ApiBinding(
            "CO.CostCenter", "API_COSTCENTER_SRV", "ODATA_V2",
            _e("CSKS", "A_CostCenter", {"KOKRS": "ControllingArea", "KOSTL": "CostCenter", "BUKRS": "CompanyCode", "KTEXT": "CostCenterName", "PRCTR": "ProfitCenter", "DATBI": "ValidityEndDate"}, ("ControllingArea", "CostCenter"), updatable=("KTEXT", "PRCTR", "DATBI")),
            on_delete="BLOCK", deep_insert=False,
        ),
        ApiBinding(
            "CO.ProfitCenter", "API_PROFITCENTER_SRV", "ODATA_V2",
            _e("CEPC", "A_ProfitCenter", {"KOKRS": "ControllingArea", "PRCTR": "ProfitCenter", "KTEXT": "ProfitCenterName", "BUKRS": "CompanyCode"}, ("ControllingArea", "ProfitCenter"), updatable=("KTEXT",)),
            on_delete="BLOCK", deep_insert=False,
        ),
        ApiBinding(
            "PP.ProductionOrder", "API_PRODUCTION_ORDER_2_SRV", "ODATA_V2",
            _e("AFKO", "A_ProductionOrder_2", {"AUFNR": "ManufacturingOrder"}, ("ManufacturingOrder",)),
            {},
            on_delete="FORBIDDEN", deep_insert=False,
            history_rule="only open production orders are re-created",
            notes="generic binding: fields beyond the key travel as extension properties until the order structure is mapped",
        ),
    ]
}


# Read-only bindings: tables the reconciliation reads back from the target through released read services, but
# never loads (values the target derives from its own postings). Caveat, stronger than above: no public reference
# for a released on-premise OData read service of asset values was found (the public cloud lists
# API_FIXEDASSET_G4BA for master data and the CDS view I_AssetValuationForLedger for values); this binding is a
# placeholder the metadata check will confirm or refute on the target, and the asset check reports "not readable"
# whenever the service is absent.
READ_BINDINGS: dict[str, EntityBinding] = {
    "ANLC": _e("ANLC", "FixedAssetValuation", {"BUKRS": "CompanyCode", "ANLN1": "MasterFixedAsset", "ANLN2": "FixedAsset", "GJAHR": "FiscalYear", "AFABE": "AssetDepreciationArea", "KANSW": "AcquisitionValueAmount", "KNAFA": "AccumulatedDepreciationAmount", "NAFAG": "DepreciationAmountInFiscalYear"}, ("CompanyCode", "MasterFixedAsset", "FixedAsset", "FiscalYear", "AssetDepreciationArea")),
}
READ_SERVICES: dict[str, dict[str, EntityBinding]] = {"API_FIXEDASSET": {"FixedAssetValuation": READ_BINDINGS["ANLC"]}}
READ_SERVICE_OF: dict[str, str] = {"ANLC": "API_FIXEDASSET"}


def binding_for(object_type: str) -> ApiBinding | None:
    return API_BINDINGS.get(object_type)
