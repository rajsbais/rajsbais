"""Canonical (simulated) SAP ECC data dictionary subset used by the MVP.

Only the tables/fields needed for the selective-refresh vertical slice are modelled.
Field lists are a *reduced* view of the real DDIC structures. A real adapter must
read DD02L/DD03L (or the ABAP agent's metadata API) instead of this static map.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TableDef:
    name: str
    description: str
    keys: tuple[str, ...]
    fields: tuple[str, ...]
    config: bool = False  # customizing: verified in target, never copied by selective refresh


def _t(name, desc, keys, fields, config=False) -> TableDef:
    return TableDef(name, desc, tuple(keys), tuple(fields), config)


TABLES: dict[str, TableDef] = {
    t.name: t
    for t in [
        _t("T001", "Company codes", ["BUKRS"], ["BUKRS", "BUTXT", "ORT01", "WAERS"], True),
        _t("T001W", "Plants", ["WERKS"], ["WERKS", "NAME1", "BUKRS", "LAND1"], True),
        _t("KNA1", "Customer master (general)", ["KUNNR"],
           ["KUNNR", "NAME1", "ORT01", "PSTLZ", "STRAS", "TELF1", "STCD1", "ADRNR", "LAND1", "ZZ_CONTACT_EMAIL"]),
        _t("KNB1", "Customer master (company code)", ["KUNNR", "BUKRS"], ["KUNNR", "BUKRS", "AKONT"]),
        _t("KNVV", "Customer master (sales area)", ["KUNNR", "VKORG", "VTWEG", "SPART"],
           ["KUNNR", "VKORG", "VTWEG", "SPART", "KDGRP"]),
        # Simplified: keyed by bank-detail ID (as BUT0BANK-BKVID in S/4) so the account number is not part of the key
        # and can be masked. In ECC the real KNBK key includes BANKN; masking it needs key remapping (not implemented).
        _t("KNBK", "Customer bank details", ["KUNNR", "BKVID"],
           ["KUNNR", "BKVID", "BANKS", "BANKL", "BANKN", "IBAN", "KOINH"]),
        _t("LFA1", "Vendor master (general)", ["LIFNR"],
           ["LIFNR", "NAME1", "ORT01", "TELF1", "STCD1", "ADRNR", "LAND1"]),
        _t("LFB1", "Vendor master (company code)", ["LIFNR", "BUKRS"], ["LIFNR", "BUKRS", "AKONT"]),
        _t("LFBK", "Vendor bank details", ["LIFNR", "BKVID"],
           ["LIFNR", "BKVID", "BANKS", "BANKL", "BANKN", "IBAN", "KOINH"]),
        _t("ADRC", "Business address", ["ADDRNUMBER"],
           ["ADDRNUMBER", "NAME1", "STREET", "CITY1", "POST_CODE1", "TEL_NUMBER", "SMTP_ADDR"]),
        _t("MARA", "Material master (general)", ["MATNR"], ["MATNR", "MTART", "MATKL", "MEINS", "ERSDA"]),
        _t("MAKT", "Material descriptions", ["MATNR", "SPRAS"], ["MATNR", "SPRAS", "MAKTX"]),
        _t("MARC", "Material master (plant)", ["MATNR", "WERKS"], ["MATNR", "WERKS", "DISMM"]),
        _t("VBAK", "Sales document header", ["VBELN"],
           ["VBELN", "ERDAT", "AUART", "VKORG", "BUKRS_VF", "KUNNR", "NETWR", "WAERK", "ERNAM"]),
        _t("VBAP", "Sales document item", ["VBELN", "POSNR"],
           ["VBELN", "POSNR", "MATNR", "WERKS", "KWMENG", "NETWR"]),
        _t("LIKP", "Delivery header", ["VBELN"], ["VBELN", "ERDAT", "KUNNR", "WERKS"]),
        _t("LIPS", "Delivery item", ["VBELN", "POSNR"],
           ["VBELN", "POSNR", "MATNR", "WERKS", "LFIMG", "VGBEL", "VGPOS"]),
        _t("VBRK", "Billing document header", ["VBELN"],
           ["VBELN", "FKDAT", "BUKRS", "KUNRG", "NETWR", "WAERK"]),
        _t("VBRP", "Billing document item", ["VBELN", "POSNR"],
           ["VBELN", "POSNR", "MATNR", "FKIMG", "NETWR", "VGBEL", "AUBEL", "AUPOS"]),
        _t("VBFA", "Sales document flow", ["VBELV", "POSNV", "VBELN", "POSNN"],
           ["VBELV", "POSNV", "VBELN", "POSNN", "VBTYP_N"]),
        _t("BKPF", "Accounting document header", ["BUKRS", "BELNR", "GJAHR"],
           ["BUKRS", "BELNR", "GJAHR", "BLDAT", "BUDAT", "BLART", "AWTYP", "AWKEY", "WAERS"]),
        _t("BSEG", "Accounting document segment", ["BUKRS", "BELNR", "GJAHR", "BUZEI"],
           ["BUKRS", "BELNR", "GJAHR", "BUZEI", "KOART", "SHKZG", "DMBTR", "KUNNR", "LIFNR", "HKONT"]),
        _t("EKKO", "Purchasing document header", ["EBELN"],
           ["EBELN", "BUKRS", "LIFNR", "BEDAT", "EKORG", "BSART"]),
        _t("EKPO", "Purchasing document item", ["EBELN", "EBELP"],
           ["EBELN", "EBELP", "MATNR", "WERKS", "MENGE", "NETPR"]),
        # Manufacturing (PP) and inventory management (MM-IM). Reduced views: simplified keys, no work-centre master, costing or batches.
        _t("STKO", "BOM header (simplified: material and plant on the header, as MAST+STKO)", ["STLNR"], ["STLNR", "MATNR", "WERKS", "STLAN", "BMENG", "DATUV"]),
        _t("STPO", "BOM item", ["STLNR", "STLKN"], ["STLNR", "STLKN", "IDNRK", "MENGE", "MEINS"]),
        _t("AUFK", "Order master data", ["AUFNR"], ["AUFNR", "AUART", "ERDAT", "BUKRS", "WERKS", "ERNAM"]),
        _t("AFKO", "Production order header", ["AUFNR"], ["AUFNR", "GAMNG", "GMEIN", "GSTRP", "GLTRP", "STLNR", "PLNNR"]),
        _t("AFPO", "Production order item", ["AUFNR", "POSNR"], ["AUFNR", "POSNR", "MATNR", "PSMNG", "WEMNG", "WERKS"]),
        _t("RESB", "Order component reservation (simplified key AUFNR/RSPOS)", ["AUFNR", "RSPOS"], ["AUFNR", "RSPOS", "MATNR", "WERKS", "BDMNG", "ENMNG"]),
        # Routings (task lists) and what orders and shop-floor confirmations take from them. Simplified keys: the real ones are task-list type /
        # group / counter (PLKO, PLPO), order routing number / counter (AFVC) and confirmation number / counter (AFRU).
        _t("PLKO", "Routing header (simplified: group number, material and plant on the header, as MAPL+PLKO)", ["PLNNR"], ["PLNNR", "PLNAL", "MATNR", "WERKS", "DATUV"]),
        _t("PLPO", "Routing operation (simplified key PLNNR/VORNR)", ["PLNNR", "VORNR"], ["PLNNR", "VORNR", "ARBPL", "STEUS", "LTXA1", "VGW01"]),
        _t("AFVC", "Order operation (copied from the routing when the order is created; simplified key AUFNR/VORNR)", ["AUFNR", "VORNR"],
           ["AUFNR", "VORNR", "ARBPL", "STEUS", "LTXA1", "VGW01"]),
        _t("AFRU", "Order confirmation (simplified key AUFNR/VORNR/RMZHL)", ["AUFNR", "VORNR", "RMZHL"], ["AUFNR", "VORNR", "RMZHL", "RUECK", "LMNGA", "ISM01", "BUDAT", "ERNAM"]),
        # ECC material documents: header + item. In S/4HANA these are views on MATDOC, so MKPF/MSEG hold no rows there.
        _t("MKPF", "Material document header", ["MBLNR", "MJAHR"], ["MBLNR", "MJAHR", "BLDAT", "BUDAT", "USNAM"]),
        _t("MSEG", "Material document item", ["MBLNR", "MJAHR", "ZEILE"],
           ["MBLNR", "MJAHR", "ZEILE", "BWART", "MATNR", "WERKS", "BUKRS", "MENGE", "MEINS", "DMBTR", "AUFNR"]),
        # S/4HANA-only tables (absent in ECC)
        _t("MATDOC", "Material document (S/4HANA: header and item in one table)", ["MBLNR", "MJAHR", "ZEILE"],
           ["MBLNR", "MJAHR", "ZEILE", "BLDAT", "BUDAT", "USNAM", "BWART", "MATNR", "WERKS", "BUKRS", "MENGE", "MEINS", "DMBTR", "AUFNR"]),
        _t("BUT000", "Business partner (general)", ["PARTNER"], ["PARTNER", "BU_GROUP", "NAME_ORG1", "BU_SORT1", "TYPE"]),
        _t("ACDOCA", "Universal journal entry", ["RLDNR", "RBUKRS", "GJAHR", "BELNR", "DOCLN"],
           ["RLDNR", "RBUKRS", "GJAHR", "BELNR", "DOCLN", "RACCT", "HSL", "KUNNR", "AWTYP", "AWREF"]),
        # Quality management (QM): inspection lots with their characteristics, results and usage decision. Simplified keys and fields; no sample
        # management, inspection plans, certificates or defects.
        _t("QALS", "Inspection lot", ["PRUEFLOS"], ["PRUEFLOS", "ART", "MATNR", "WERKS", "AUFNR", "LOSMENGE", "MEINS", "ENSTEHDAT"]),
        _t("QAMV", "Inspection characteristic of a lot", ["PRUEFLOS", "MERKNR"], ["PRUEFLOS", "MERKNR", "KURZTEXT", "SOLLWERT", "TOLUNL", "TOLOBL"]),
        _t("QASR", "Inspection result for a characteristic", ["PRUEFLOS", "MERKNR", "PROBENR"], ["PRUEFLOS", "MERKNR", "PROBENR", "MESSWERT", "PRUEFER", "PRUEFDATUV"]),
        _t("QAVE", "Usage decision of a lot", ["PRUEFLOS"], ["PRUEFLOS", "VCODE", "VDATUM", "VAENAME"]),
        # Plant maintenance (PM): functional locations, equipment and maintenance notifications. Simplified: the location data that SAP keeps in
        # ILOA/IFLOS is folded into the headers; no maintenance orders, task lists, measuring points or warranties.
        _t("IFLOT", "Functional location", ["TPLNR"], ["TPLNR", "FLTYP", "SWERK", "TPLMA", "PLTXT", "ERDAT"]),
        _t("EQUI", "Equipment master", ["EQUNR"], ["EQUNR", "EQART", "HERST", "SERGE", "MATNR", "TPLNR", "SWERK", "ANSDT", "ERDAT"]),
        _t("EQKT", "Equipment short text", ["EQUNR", "SPRAS"], ["EQUNR", "SPRAS", "EQKTX"]),
        _t("QMEL", "Maintenance notification", ["QMNUM"], ["QMNUM", "QMART", "EQUNR", "TPLNR", "QMTXT", "QMDAT", "SWERK", "ERNAM"]),
        _t("AFIH", "Maintenance order header (PM part of the order; simplified)", ["AUFNR"], ["AUFNR", "ILART", "EQUNR", "TPLNR", "QMNUM", "PRIOK", "GSTRP"]),
        # HR master data (infotypes), simplified keys: the real ones also carry object, lock and sequence fields. Special-category personal data:
        # see masking/engine.py (HR_TABLES) and the hr:copy permission.
        _t("PA0003", "HR master record: core data (one row per personnel number)", ["PERNR"], ["PERNR", "ABKRS", "ERDAT"]),
        _t("PA0001", "HR infotype 0001: organizational assignment", ["PERNR", "ENDDA"], ["PERNR", "ENDDA", "BEGDA", "BUKRS", "WERKS", "PERSG", "ORGEH", "STELL", "KOSTL"]),
        _t("PA0002", "HR infotype 0002: personal data", ["PERNR", "ENDDA"], ["PERNR", "ENDDA", "BEGDA", "NACHN", "VORNA", "GBDAT", "GESCH", "NATIO", "PERID"]),
        _t("PA0006", "HR infotype 0006: addresses", ["PERNR", "SUBTY", "ENDDA"], ["PERNR", "SUBTY", "ENDDA", "BEGDA", "STRAS", "ORT01", "PSTLZ", "LAND1", "TELNR"]),
        _t("PA0008", "HR infotype 0008: basic pay", ["PERNR", "ENDDA"], ["PERNR", "ENDDA", "BEGDA", "TRFGR", "BET01", "WAERS", "ANSAL"]),
        _t("PA0009", "HR infotype 0009: bank details", ["PERNR", "SUBTY", "ENDDA"], ["PERNR", "SUBTY", "ENDDA", "BEGDA", "EMFTX", "BANKL", "BANKN"]),
        # The SAP flight demo data model (package SAPBC_DATAMODEL, generated by report SAPBC_DATA_GENERATOR): present in ABAP trial systems
        # such as NPL, which have no ERP data. Reduced to the commonly used fields; field names are from memory and are checked by the
        # smoke test against the real DDIC.
        _t("SCARR", "Airline", ["CARRID"], ["CARRID", "CARRNAME", "CURRCODE", "URL"]),
        _t("SPFLI", "Flight schedule (connection)", ["CARRID", "CONNID"],
           ["CARRID", "CONNID", "COUNTRYFR", "CITYFROM", "AIRPFROM", "COUNTRYTO", "CITYTO", "AIRPTO", "DEPTIME", "ARRTIME", "DISTANCE"]),
        _t("SFLIGHT", "Flight", ["CARRID", "CONNID", "FLDATE"],
           ["CARRID", "CONNID", "FLDATE", "PRICE", "CURRENCY", "PLANETYPE", "SEATSMAX", "SEATSOCC", "PAYMENTSUM"]),
        _t("SCUSTOM", "Flight customer", ["ID"],
           ["ID", "NAME", "FORM", "STREET", "POSTBOX", "POSTCODE", "CITY", "COUNTRY", "TELEPHONE", "CUSTTYPE", "DISCOUNT", "LANGU", "EMAIL", "WEBUSER"]),
        _t("SBOOK", "Flight booking", ["CARRID", "CONNID", "FLDATE", "BOOKID"],
           ["CARRID", "CONNID", "FLDATE", "BOOKID", "CUSTOMID", "CUSTTYPE", "SMOKER", "LUGGWEIGHT", "WUNIT", "INVOICE", "CLASS", "FORCURAM", "FORCURKEY",
            "LOCCURAM", "LOCCURKEY", "ORDER_DATE", "AGENCYNUM", "CANCELLED", "PASSNAME", "PASSBIRTH"]),
        _t("NRIV", "Number range intervals", ["OBJECT", "NRRANGENR"],
           ["OBJECT", "NRRANGENR", "FROMNUMBER", "TONUMBER", "NRLEVEL"], True),
    ]
}

# Header table -> number range object guarding the document number (simplified)
NUMBER_RANGE_OBJECTS: dict[str, tuple[str, str]] = {
    "VBAK": ("SD_ORDER", "VBELN"),
    "LIKP": ("SD_DELIV", "VBELN"),
    "VBRK": ("SD_BILL", "VBELN"),
    "EKKO": ("MM_PO", "EBELN"),
    "AUFK": ("PP_ORDER", "AUFNR"),
    "PLKO": ("PP_ROUT", "PLNNR"),
    "QALS": ("QM_LOT", "PRUEFLOS"),
    "EQUI": ("PM_EQUI", "EQUNR"),
    "QMEL": ("PM_NOTIF", "QMNUM"),
    "MKPF": ("MM_MBLNR", "MBLNR"),
    "MATDOC": ("MM_MBLNR", "MBLNR"),
}


def key_of(table: str, row: dict) -> tuple:
    return tuple(row[k] for k in TABLES[table].keys)


def key_str(table: str, row: dict) -> str:
    return "/".join(str(v) for v in key_of(table, row))
