"""Minimal SAP-like table registry (DDIC subset) used by discovery, rule validation and extraction.

Field lists are deliberately compact. They mirror public SAP field naming so that the engine is SAP-aware,
but no proprietary DDIC content is reproduced. Custom (Z/Y) tables are recognised by prefix.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TableDef:
    name: str
    description: str
    key_fields: tuple[str, ...]
    fields: tuple[str, ...]
    org_field: str | None = None  # BUKRS / WERKS / None
    year_field: str | None = None
    domain: str = "CONFIG"
    avg_row_bytes: int = 256
    s4_status: str = "RETAINED"  # RETAINED | REPLACED | COMPATIBILITY_VIEW | REMOVED
    s4_note: str = ""


def _t(name, desc, keys, fields, org=None, year=None, domain="CONFIG", rb=256, s4="RETAINED", note=""):
    return TableDef(name, desc, tuple(keys), tuple(keys) + tuple(f for f in fields if f not in keys), org, year, domain, rb, s4, note)


TABLES: dict[str, TableDef] = {
    t.name: t
    for t in [
        _t("T000", "Clients", ["MANDT"], ["MTEXT", "LOGSYS"]),
        _t("T001", "Company codes", ["BUKRS"], ["BUTXT", "LAND1", "WAERS", "KTOPL", "PERIV", "SPRAS"]),
        _t("T001W", "Plants", ["WERKS"], ["NAME1", "BWKEY", "LAND1", "VKORG", "EKORG"], org="WERKS"),
        _t("T001K", "Valuation areas", ["BWKEY"], ["BUKRS"], org="BUKRS"),
        _t("TVKO", "Sales organisations", ["VKORG"], ["BUKRS", "VTEXT"], org="BUKRS"),
        _t("T024E", "Purchasing organisations", ["EKORG"], ["BUKRS", "EKOTX"], org="BUKRS"),
        _t("TKA01", "Controlling areas", ["KOKRS"], ["BEZEI", "WAERS", "KTOPL"]),
        _t("TKA02", "Company code to controlling area", ["BUKRS"], ["KOKRS"], org="BUKRS"),
        _t("T004", "Charts of accounts", ["KTOPL"], ["KTPLT"]),
        _t("SKA1", "G/L account master (chart)", ["KTOPL", "SAKNR"], ["TXT50", "XBILK", "GVTYP"], domain="FI"),
        _t("SKB1", "G/L account master (company code)", ["BUKRS", "SAKNR"], ["MITKZ", "XOPVW"], org="BUKRS", domain="FI"),
        _t("CSKS", "Cost centers", ["KOKRS", "KOSTL"], ["BUKRS", "KTEXT", "PRCTR", "DATBI"], org="BUKRS", domain="CO"),
        _t("CEPC", "Profit centers", ["KOKRS", "PRCTR"], ["KTEXT", "BUKRS"], org="BUKRS", domain="CO"),
        _t("KNA1", "Customer general", ["KUNNR"], ["NAME1", "LAND1", "VBUND", "KTOKD"], domain="SD", s4="COMPATIBILITY_VIEW", note="Business Partner (CVI) is the leading object in S/4HANA"),
        _t("KNB1", "Customer company code", ["KUNNR", "BUKRS"], ["AKONT", "ZTERM"], org="BUKRS", domain="FI", s4="COMPATIBILITY_VIEW", note="Loaded via Business Partner API"),
        _t("LFA1", "Vendor general", ["LIFNR"], ["NAME1", "LAND1", "VBUND", "KTOKK"], domain="MM", s4="COMPATIBILITY_VIEW", note="Business Partner (CVI) is the leading object in S/4HANA"),
        _t("LFB1", "Vendor company code", ["LIFNR", "BUKRS"], ["AKONT", "ZTERM"], org="BUKRS", domain="FI", s4="COMPATIBILITY_VIEW", note="Loaded via Business Partner API"),
        _t("MARA", "Material general", ["MATNR"], ["MTART", "MATKL", "MEINS", "MAKTX"], domain="MM", note="MATNR length 40 in S/4HANA"),
        _t("MARC", "Material plant", ["MATNR", "WERKS"], ["DISPO", "EKGRP", "BESKZ"], org="WERKS", domain="MM"),
        _t("MBEW", "Material valuation", ["MATNR", "BWKEY", "BWTAR"], ["VPRSV", "VERPR", "STPRS", "LBKUM", "SALK3", "WAERS"], domain="MM", note="Material Ledger mandatory in S/4HANA (ML_DATA)"),
        _t("MARD", "Storage location stock", ["MATNR", "WERKS", "LGORT"], ["LABST"], org="WERKS", domain="MM", s4="COMPATIBILITY_VIEW", note="Stock quantities derived from MATDOC in S/4HANA"),
        _t("ANLA", "Asset master", ["BUKRS", "ANLN1", "ANLN2"], ["TXT50", "ANLKL", "KOSTL", "AKTIV"], org="BUKRS", domain="FI-AA", note="New Asset Accounting mandatory; values in ACDOCA"),
        _t("ANLC", "Asset values", ["BUKRS", "ANLN1", "ANLN2", "GJAHR", "AFABE"], ["KANSW", "KNAFA", "NAFAG"], org="BUKRS", year="GJAHR", domain="FI-AA", s4="REPLACED", note="Replaced by ACDOCA/FAAT_DOC_IT"),
        _t("VBAK", "Sales order header", ["VBELN"], ["AUART", "VKORG", "VTWEG", "SPART", "KUNNR", "AUDAT", "WAERK", "NETWR", "BUKRS_VF", "GBSTK"], org="BUKRS_VF", domain="SD", rb=512),
        _t("VBAP", "Sales order item", ["VBELN", "POSNR"], ["MATNR", "WERKS", "KWMENG", "NETWR", "PRCTR"], org="WERKS", domain="SD", rb=640),
        _t("VBFA", "Sales document flow", ["VBELV", "POSNV", "VBELN", "POSNN", "VBTYP_N"], ["VBTYP_V", "RFMNG"], domain="SD"),
        _t("LIKP", "Delivery header", ["VBELN"], ["LFART", "VSTEL", "KUNNR", "WADAT_IST", "WERKS", "BUKRS"], org="BUKRS", domain="SD", rb=512),
        _t("LIPS", "Delivery item", ["VBELN", "POSNR"], ["MATNR", "WERKS", "LFIMG", "VGBEL", "VGPOS"], org="WERKS", domain="SD"),
        _t("VBRK", "Billing header", ["VBELN"], ["FKART", "VKORG", "KUNRG", "BUKRS", "FKDAT", "WAERK", "NETWR", "RFBSK", "GJAHR"], org="BUKRS", year="GJAHR", domain="SD", rb=512),
        _t("VBRP", "Billing item", ["VBELN", "POSNR"], ["MATNR", "WERKS", "FKIMG", "NETWR", "VGBEL", "VGPOS", "AUBEL", "AUPOS"], org="WERKS", domain="SD"),
        _t("EKKO", "Purchase order header", ["EBELN"], ["BUKRS", "BSTYP", "BSART", "LIFNR", "EKORG", "BEDAT", "WAERS", "GJAHR"], org="BUKRS", year="GJAHR", domain="MM", rb=512),
        _t("EKPO", "Purchase order item", ["EBELN", "EBELP"], ["MATNR", "WERKS", "MENGE", "NETPR", "NETWR", "ELIKZ"], org="WERKS", domain="MM"),
        _t("EKBE", "PO history", ["EBELN", "EBELP", "ZEKKN", "VGABE", "GJAHR", "BELNR", "BUZEI"], ["BWART", "MENGE", "DMBTR", "BEWTP"], year="GJAHR", domain="MM"),
        _t("MKPF", "Material document header", ["MBLNR", "MJAHR"], ["BLDAT", "BUDAT", "TCODE2", "XBLNR"], year="MJAHR", domain="MM", s4="COMPATIBILITY_VIEW", note="MATDOC is the leading table in S/4HANA"),
        _t("MSEG", "Material document item", ["MBLNR", "MJAHR", "ZEILE"], ["BWART", "MATNR", "WERKS", "LGORT", "MENGE", "DMBTR", "BUKRS", "EBELN", "EBELP", "AUFNR", "UMWRK", "SHKZG"], org="BUKRS", year="MJAHR", domain="MM", rb=640, s4="COMPATIBILITY_VIEW", note="MATDOC is the leading table in S/4HANA"),
        _t("RBKP", "Invoice receipt header", ["BELNR", "GJAHR"], ["BUKRS", "LIFNR", "BLDAT", "RMWWR", "WAERS", "RBSTAT"], org="BUKRS", year="GJAHR", domain="MM"),
        _t("RSEG", "Invoice receipt item", ["BELNR", "GJAHR", "BUZEI"], ["EBELN", "EBELP", "MATNR", "WERKS", "WRBTR", "MENGE"], year="GJAHR", domain="MM"),
        _t("BKPF", "Accounting document header", ["BUKRS", "BELNR", "GJAHR"], ["BLART", "BLDAT", "BUDAT", "MONAT", "WAERS", "AWTYP", "AWKEY", "BVORG", "XBLNR", "BSTAT"], org="BUKRS", year="GJAHR", domain="FI", rb=384),
        _t("BSEG", "Accounting document line", ["BUKRS", "BELNR", "GJAHR", "BUZEI"], ["KOART", "SHKZG", "HKONT", "DMBTR", "WRBTR", "KUNNR", "LIFNR", "KOSTL", "PRCTR", "AUGBL", "AUGDT", "VBUND", "MATNR", "WERKS"], org="BUKRS", year="GJAHR", domain="FI", rb=768, s4="COMPATIBILITY_VIEW", note="Universal Journal ACDOCA is the leading table; BSEG retained for open-item fields"),
        _t("BSID", "Customer open items", ["BUKRS", "KUNNR", "UMSKS", "UMSKZ", "AUGDT", "AUGBL", "ZUONR", "GJAHR", "BELNR", "BUZEI"], ["DMBTR", "SHKZG"], org="BUKRS", year="GJAHR", domain="FI", s4="COMPATIBILITY_VIEW", note="Replaced by ACDOCA open item views"),
        _t("BSIK", "Vendor open items", ["BUKRS", "LIFNR", "UMSKS", "UMSKZ", "AUGDT", "AUGBL", "ZUONR", "GJAHR", "BELNR", "BUZEI"], ["DMBTR", "SHKZG"], org="BUKRS", year="GJAHR", domain="FI", s4="COMPATIBILITY_VIEW", note="Replaced by ACDOCA open item views"),
        _t("AFKO", "Production order header", ["AUFNR"], ["PLNBEZ", "GAMNG", "GSTRP", "GLTRP", "DWERK"], org="DWERK", domain="PP"),
        _t("AFPO", "Production order item", ["AUFNR", "POSNR"], ["MATNR", "PSMNG", "WEMNG", "DWERK"], org="DWERK", domain="PP"),
        _t("AFRU", "Order confirmations", ["RUECK", "RMZHL"], ["AUFNR", "LMNGA", "ISM01", "WERKS", "BUDAT"], org="WERKS", domain="PP"),
        _t("AUFK", "Order master (settlement)", ["AUFNR"], ["AUART", "KOKRS", "BUKRS", "WERKS", "KOSTL", "PRCTR"], org="BUKRS", domain="CO"),
        _t("RFCDES", "RFC destinations", ["RFCDEST"], ["RFCTYPE", "RFCHOST", "RFCOPTIONS"]),
        _t("EDPP1", "Partner profiles (IDoc)", ["PARNUM", "PARTYP"], ["RCVPOR", "MESTYP"]),
        _t("TBTCO", "Background jobs", ["JOBNAME", "JOBCOUNT"], ["SDLUNAME", "STATUS", "PERIODIC"]),
        _t("ZSD_EXPORT_CTRL", "Custom: export-control classification", ["MATNR"], ["ECCN", "CONTROLLED", "LICENSE_REQ"], domain="SD"),
        _t("ZFI_TSA_SCOPE", "Custom: TSA service scope", ["BUKRS", "TSA_ID"], ["SERVICE", "END_DATE"], org="BUKRS", domain="FI"),
        _t("ZMM_SUPPLIER_EXT", "Custom: supplier extension", ["LIFNR"], ["RISK_RATING", "ESG_SCORE"], domain="MM"),
    ]
}


def is_custom(table_name: str) -> bool:
    return table_name.upper().startswith(("Z", "Y"))


def table_def(name: str) -> TableDef | None:
    return TABLES.get(name)


def record_key(table: str, row: dict) -> str:
    td = TABLES[table]
    return "|".join(str(row.get(k, "")) for k in td.key_fields)
