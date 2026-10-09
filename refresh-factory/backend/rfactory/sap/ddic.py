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
}


def key_of(table: str, row: dict) -> tuple:
    return tuple(row[k] for k in TABLES[table].keys)


def key_str(table: str, row: dict) -> str:
    return "/".join(str(v) for v in key_of(table, row))
