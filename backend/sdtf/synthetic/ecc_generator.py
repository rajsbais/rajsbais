"""Deterministic synthetic ECC 6.0-like landscape generator.

Produces SAP-shaped rows (public field names, no proprietary content) for a multi-company-code group
including the patterns a carve-out engine must handle: shared customers/vendors/materials, cross-company
sales (intercompany billing), cross-company purchase orders, cross-company stock transfers, intercompany
FI postings (BVORG), open and cleared items, custom Z tables, interfaces and jobs.

All amounts are generated so that every accounting document balances per company code, which lets the
reconciliation layer assert trial-balance integrity on both source and target.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..catalog.tables import TABLES, record_key


@dataclass
class CompanyCodeSpec:
    bukrs: str
    name: str
    country: str
    currency: str
    kokrs: str
    plants: list[str]
    ktopl: str = "INT"


@dataclass
class LandscapeSpec:
    seed: int = 42
    sid: str = "ECP"
    client: str = "100"
    fiscal_years: tuple[int, ...] = (2022, 2023, 2024, 2025)
    scale: int = 1  # multiplies document volumes
    company_codes: list[CompanyCodeSpec] = field(default_factory=lambda: DEFAULT_COMPANY_CODES.copy())
    shared_customer_ratio: float = 0.25
    shared_vendor_ratio: float = 0.25
    shared_material_ratio: float = 0.35
    cross_company_sales_ratio: float = 0.10
    cross_company_po_ratio: float = 0.08
    export_controlled_ratio: float = 0.06


DEFAULT_COMPANY_CODES: list[CompanyCodeSpec] = [
    CompanyCodeSpec("1000", "Nordlicht Holding AG", "DE", "EUR", "1000", ["1010", "1020"]),
    CompanyCodeSpec("1100", "Nordlicht Produktion GmbH", "DE", "EUR", "1000", ["1110"]),
    CompanyCodeSpec("2000", "Nordlicht Americas Inc.", "US", "USD", "2000", ["2010", "2020"]),
    CompanyCodeSpec("3000", "Nordlicht UK Ltd.", "GB", "GBP", "1000", ["3010"]),
    CompanyCodeSpec("4000", "Nordlicht Asia Pte. Ltd.", "SG", "SGD", "1000", ["4010"]),
    CompanyCodeSpec("5000", "Nordlicht Specialty Materials GmbH", "DE", "EUR", "1000", ["5010", "5020"]),
]

GL_ACCOUNTS = {
    "140000": ("Trade receivables - domestic", "X", ""),
    "141000": ("Receivables affiliated companies", "X", ""),
    "160000": ("Trade payables - domestic", "X", ""),
    "161000": ("Payables affiliated companies", "X", ""),
    "191100": ("GR/IR clearing", "X", ""),
    "300000": ("Raw materials inventory", "X", ""),
    "310000": ("Finished goods inventory", "X", ""),
    "113100": ("Bank - main account", "X", ""),
    "800000": ("Revenue - domestic", "", "X"),
    "801000": ("Revenue - intercompany", "", "X"),
    "400000": ("Consumption raw materials", "", "X"),
    "410000": ("Intercompany expense", "", "X"),
    "420000": ("Payroll and salaries", "", "X"),
    "470000": ("Other operating expense", "", "X"),
    "211000": ("Fixed assets - machinery", "X", ""),
    "220000": ("Accumulated depreciation", "X", ""),
    "480000": ("Depreciation expense", "", "X"),
    "893000": ("Cost of goods sold", "", "X"),
    "895000": ("Production output", "", "X"),
    "700000": ("Share capital", "X", ""),
}

MATERIAL_TYPES = ["ROH", "HALB", "FERT", "HAWA"]
CUSTOMER_NAMES = ["Aurora Retail", "Baltic Foods", "Cobalt Engineering", "Delta Logistics", "Everest Pharma", "Fjord Marine",
                  "Granite Construction", "Helios Energy", "Indigo Textiles", "Juniper Health", "Kestrel Aerospace", "Lumen Optics",
                  "Meridian Chemicals", "Nimbus Cloud", "Orion Automotive", "Pacific Timber", "Quartz Labs", "Riviera Hotels",
                  "Sable Mining", "Tundra Outdoor", "Umbra Security", "Vertex Robotics", "Willow Agri", "Xenon Lighting", "Yarrow Organics", "Zephyr Airlines"]
VENDOR_NAMES = ["Alpine Steel", "Borealis Plastics", "Carbon Fibre Works", "Dune Packaging", "Ember Electronics", "Flux Chemicals",
                "Glacier Water", "Harbor Freight", "Iris Components", "Jade Minerals", "Krypton Gases", "Lotus Paper", "Mica Coatings",
                "Nova Tools", "Opal Glass", "Pyrite Alloys", "Quill Office", "Rhodium Metals", "Sierra Transport", "Topaz Semiconductors"]


class Counter:
    def __init__(self, start: int):
        self.v = start

    def next(self) -> str:
        self.v += 1
        return str(self.v)


class EccLandscapeGenerator:
    def __init__(self, spec: LandscapeSpec | None = None):
        self.spec = spec or LandscapeSpec()
        self.rng = random.Random(self.spec.seed)
        self.tables: dict[str, list[dict]] = {name: [] for name in TABLES}
        self._keys: dict[str, set[str]] = {name: set() for name in TABLES}
        self.cc_by_code = {c.bukrs: c for c in self.spec.company_codes}
        self.plant_cc: dict[str, str] = {}
        self.cc_customers: dict[str, list[str]] = {}
        self.cc_vendors: dict[str, list[str]] = {}
        self.cc_affiliate_customer: dict[tuple[str, str], str] = {}  # (bukrs, partner_bukrs) -> KUNNR
        self.cc_affiliate_vendor: dict[tuple[str, str], str] = {}
        self.plant_materials: dict[str, list[str]] = {}
        self.cc_costcenters: dict[str, list[str]] = {}
        self.cc_profitcenters: dict[str, list[str]] = {}
        self.counters = {
            "VBELN_SO": Counter(1000000), "VBELN_DL": Counter(80000000), "VBELN_BI": Counter(90000000),
            "EBELN": Counter(4500000000), "MBLNR": Counter(4900000000), "RBKP": Counter(5105600000),
            "BELNR": Counter(100000000), "AUFNR": Counter(1000100), "RUECK": Counter(10000), "BVORG": Counter(0),
            "STLNR": Counter(1000), "PLNNR": Counter(50000000), "OBJID": Counter(10000000), "CHARG": Counter(1000000),
        }
        self.plant_work_centers: dict[str, list[str]] = {}
        self.cc_contracts: dict[str, list[dict]] = {}

    # ---------------------------------------------------------------- helpers
    def add(self, table: str, row: dict) -> dict:
        key = record_key(table, row)
        if key in self._keys[table]:
            return row
        self._keys[table].add(key)
        self.tables[table].append(row)
        return row

    def _amount(self, lo: float, hi: float) -> float:
        return round(self.rng.uniform(lo, hi), 2)

    def _date(self, year: int) -> date:
        d0 = date(year, 1, 1)
        return d0 + timedelta(days=self.rng.randint(0, 364 if year < 2025 else 250))

    def _ds(self, d: date) -> str:
        return d.strftime("%Y%m%d")

    # ---------------------------------------------------------------- build
    def generate(self) -> dict[str, list[dict]]:
        self._config()
        self._gl_accounts()
        self._cost_objects()
        self._partners()
        self._work_centers()
        self._materials()
        self._manufacturing_masters()
        self._assets()
        self._contracts()
        for cc in self.spec.company_codes:
            for year in self.spec.fiscal_years:
                self._sales_cycle(cc, year)
                self._procurement_cycle(cc, year)
                self._scheduling_agreements(cc, year)
                self._production_cycle(cc, year)
                self._manual_fi(cc, year)
        self._intercompany_postings()
        self._cross_company_stock_transfers()
        self._interfaces_and_jobs()
        self._custom_tables()
        return self.tables

    def _config(self):
        s = self.spec
        self.add("T000", {"MANDT": s.client, "MTEXT": "Synthetic ECC production client", "LOGSYS": f"{s.sid}CLNT{s.client}"})
        kokrs_seen = set()
        for cc in s.company_codes:
            self.add("T001", {"BUKRS": cc.bukrs, "BUTXT": cc.name, "LAND1": cc.country, "WAERS": cc.currency, "KTOPL": cc.ktopl, "PERIV": "K4", "SPRAS": "E"})
            self.add("TKA02", {"BUKRS": cc.bukrs, "KOKRS": cc.kokrs})
            self.add("TVKO", {"VKORG": cc.bukrs, "BUKRS": cc.bukrs, "VTEXT": f"Sales org {cc.name}"})
            self.add("T024E", {"EKORG": cc.bukrs, "BUKRS": cc.bukrs, "EKOTX": f"Purch org {cc.name}"})
            if cc.kokrs not in kokrs_seen:
                kokrs_seen.add(cc.kokrs)
                self.add("TKA01", {"KOKRS": cc.kokrs, "BEZEI": f"Controlling area {cc.kokrs}", "WAERS": "EUR" if cc.kokrs == "1000" else "USD", "KTOPL": cc.ktopl})
            self.add("T004", {"KTOPL": cc.ktopl, "KTPLT": "Group chart of accounts"})
            for p in cc.plants:
                self.plant_cc[p] = cc.bukrs
                self.add("T001W", {"WERKS": p, "NAME1": f"Plant {p} ({cc.country})", "BWKEY": p, "LAND1": cc.country, "VKORG": cc.bukrs, "EKORG": cc.bukrs})
                self.add("T001K", {"BWKEY": p, "BUKRS": cc.bukrs})

    def _gl_accounts(self):
        for cc in self.spec.company_codes:
            for saknr, (txt, xbilk, gvtyp) in GL_ACCOUNTS.items():
                self.add("SKA1", {"KTOPL": cc.ktopl, "SAKNR": saknr, "TXT50": txt, "XBILK": xbilk, "GVTYP": gvtyp})
                self.add("SKB1", {"BUKRS": cc.bukrs, "SAKNR": saknr, "MITKZ": "D" if saknr.startswith("14") else ("K" if saknr.startswith("16") else ""), "XOPVW": "X" if saknr in ("191100",) else ""})

    def _cost_objects(self):
        for cc in self.spec.company_codes:
            pcs = [f"P{cc.bukrs}{i}" for i in range(1, 3)]
            self.cc_profitcenters[cc.bukrs] = pcs
            for pc in pcs:
                self.add("CEPC", {"KOKRS": cc.kokrs, "PRCTR": pc, "KTEXT": f"Profit center {pc}", "BUKRS": cc.bukrs})
            ccs = [f"{cc.bukrs}{i:02d}" for i in range(1, 5)]
            self.cc_costcenters[cc.bukrs] = ccs
            for i, kostl in enumerate(ccs):
                self.add("CSKS", {"KOKRS": cc.kokrs, "KOSTL": kostl, "BUKRS": cc.bukrs, "KTEXT": f"Cost center {kostl}", "PRCTR": pcs[i % 2], "DATBI": "99991231"})

    def _partners(self):
        rng = self.rng
        ccs = self.spec.company_codes
        # external customers
        kunnr = 100000
        for name in CUSTOMER_NAMES:
            kunnr += 1
            k = str(kunnr)
            shared = rng.random() < self.spec.shared_customer_ratio
            homes = rng.sample(ccs, 2 if shared and len(ccs) > 1 else 1)
            self.add("KNA1", {"KUNNR": k, "NAME1": name, "LAND1": homes[0].country, "VBUND": "", "KTOKD": "KUNA"})
            for h in homes:
                self.add("KNB1", {"KUNNR": k, "BUKRS": h.bukrs, "AKONT": "140000", "ZTERM": "Z030"})
                self.cc_customers.setdefault(h.bukrs, []).append(k)
        lifnr = 200000
        for name in VENDOR_NAMES:
            lifnr += 1
            v = str(lifnr)
            shared = rng.random() < self.spec.shared_vendor_ratio
            homes = rng.sample(ccs, 2 if shared and len(ccs) > 1 else 1)
            self.add("LFA1", {"LIFNR": v, "NAME1": name, "LAND1": homes[0].country, "VBUND": "", "KTOKK": "KRED"})
            for h in homes:
                self.add("LFB1", {"LIFNR": v, "BUKRS": h.bukrs, "AKONT": "160000", "ZTERM": "Z030"})
                self.cc_vendors.setdefault(h.bukrs, []).append(v)
        # affiliated (intercompany) partners: each company code has a customer and vendor record for each other company code
        ik, iv = 900000, 950000
        for a in ccs:
            for b in ccs:
                if a.bukrs == b.bukrs:
                    continue
                ik += 1
                k = str(ik)
                self.add("KNA1", {"KUNNR": k, "NAME1": f"IC {b.name}", "LAND1": b.country, "VBUND": b.bukrs, "KTOKD": "KUNI"})
                self.add("KNB1", {"KUNNR": k, "BUKRS": a.bukrs, "AKONT": "141000", "ZTERM": "Z000"})
                self.cc_affiliate_customer[(a.bukrs, b.bukrs)] = k
                iv += 1
                v = str(iv)
                self.add("LFA1", {"LIFNR": v, "NAME1": f"IC {b.name}", "LAND1": b.country, "VBUND": b.bukrs, "KTOKK": "KRDI"})
                self.add("LFB1", {"LIFNR": v, "BUKRS": a.bukrs, "AKONT": "161000", "ZTERM": "Z000"})
                self.cc_affiliate_vendor[(a.bukrs, b.bukrs)] = v

    def _materials(self):
        rng = self.rng
        plants = list(self.plant_cc.keys())
        n = 40 + 10 * self.spec.scale
        for i in range(1, n + 1):
            matnr = f"MAT-{i:05d}"
            mtart = MATERIAL_TYPES[i % len(MATERIAL_TYPES)]
            self.add("MARA", {"MATNR": matnr, "MTART": mtart, "MATKL": f"MG{i % 7:02d}", "MEINS": "EA", "MAKTX": f"Material {i} ({mtart})"})
            shared = rng.random() < self.spec.shared_material_ratio
            if shared:
                # pick plants from two different company codes
                ccs = rng.sample(self.spec.company_codes, 2)
                home_plants = [rng.choice(ccs[0].plants), rng.choice(ccs[1].plants)]
            else:
                home_plants = [rng.choice(plants)]
            for p in home_plants:
                self.add("MARC", {"MATNR": matnr, "WERKS": p, "DISPO": "001", "EKGRP": "001", "BESKZ": "F" if mtart in ("ROH", "HAWA") else "E"})
                price = self._amount(5, 500)
                qty = rng.randint(0, 500)
                cur = self.cc_by_code[self.plant_cc[p]].currency
                self.add("MBEW", {"MATNR": matnr, "BWKEY": p, "BWTAR": "", "VPRSV": "S" if mtart == "FERT" else "V", "VERPR": price, "STPRS": price, "LBKUM": qty, "SALK3": round(price * qty, 2), "WAERS": cur})
                self.add("MARD", {"MATNR": matnr, "WERKS": p, "LGORT": "0001", "LABST": qty})
                self.plant_materials.setdefault(p, []).append(matnr)
            if rng.random() < self.spec.export_controlled_ratio:
                self.add("ZSD_EXPORT_CTRL", {"MATNR": matnr, "ECCN": "3A001", "CONTROLLED": "X", "LICENSE_REQ": "X"})
        # guarantee that each company code owns at least one export-controlled material (carve-out compliance path)
        flagged = {r["MATNR"] for r in self.tables["ZSD_EXPORT_CTRL"]}
        for cc in self.spec.company_codes:
            own = [m for p in cc.plants for m in self.plant_materials.get(p, [])]
            if own and not flagged.intersection(own):
                self.add("ZSD_EXPORT_CTRL", {"MATNR": own[0], "ECCN": "3A001", "CONTROLLED": "X", "LICENSE_REQ": "X"})

    def _work_centers(self):
        """Two work centers per plant, each settling to a cost center of the plant's company code."""
        for werks, bukrs in self.plant_cc.items():
            for n in (1, 2):
                objid = str(self.counters["OBJID"].next())
                self.add("CRHD", {"OBJID": objid, "OBJTY": "A", "ARBPL": f"WC{werks}{n}", "WERKS": werks, "VERWE": "0001", "VGWTS": "SAP1", "KTEXT": f"Work center {n} plant {werks}"})
                self.add("CRCO", {"OBJID": objid, "LASET": 1, "KOKRS": self.cc_by_code[bukrs].kokrs, "KOSTL": self.cc_costcenters[bukrs][1], "ENDDA": "99991231"})
                self.plant_work_centers.setdefault(werks, []).append(objid)

    def _manufacturing_masters(self):
        """BOM and routing for every produced material (FERT / HALB) in its plants; batches for a third of the
        materials with stock. The BOM components are raw or semi-finished materials of the same plant."""
        rng = self.rng
        mtype = {r["MATNR"]: r["MTART"] for r in self.tables["MARA"]}
        for werks, mats in self.plant_materials.items():
            components = [m for m in mats if mtype[m] in ("ROH", "HALB")]
            produced = [m for m in mats if mtype[m] in ("FERT", "HALB")]
            for matnr in produced:
                comps = [c for c in components if c != matnr]
                if not comps:
                    continue
                stlnr = str(self.counters["STLNR"].next())
                self.add("MAST", {"MATNR": matnr, "WERKS": werks, "STLAN": "1", "STLNR": stlnr, "STLAL": "01"})
                self.add("STKO", {"STLNR": stlnr, "STLAL": "01", "STLTY": "M", "STLST": "01", "BMENG": 1, "BMEIN": "EA", "DATUV": "20200101"})
                for i, comp in enumerate(rng.sample(comps, min(len(comps), rng.randint(2, 3))), start=1):
                    self.add("STPO", {"STLNR": stlnr, "STLAL": "01", "STLKN": i, "STLTY": "M", "POSNR": f"{i * 10:04d}", "IDNRK": comp, "MENGE": rng.randint(1, 5), "MEINS": "EA", "POSTP": "L"})
                plnnr = str(self.counters["PLNNR"].next())
                self.add("MAPL", {"PLNNR": plnnr, "PLNTY": "N", "PLNAL": "01", "MATNR": matnr, "WERKS": werks})
                self.add("PLKO", {"PLNNR": plnnr, "PLNTY": "N", "PLNAL": "01", "WERKS": werks, "STATU": "4", "VERWE": "1", "PLNME": "EA", "LOSVN": 1, "LOSBS": 99999999, "KTEXT": f"Routing {matnr}"})
                wcs = self.plant_work_centers.get(werks, [])
                for i, op in enumerate(("Setup and assembly", "Inspection and packing"), start=1):
                    self.add("PLPO", {"PLNNR": plnnr, "PLNTY": "N", "PLNKN": i, "PLNAL": "01", "VORNR": f"{i * 10:04d}", "ARBID": wcs[(i - 1) % len(wcs)] if wcs else "", "WERKS": werks, "LTXA1": op, "VGW01": rng.randint(5, 60), "VGE01": "MIN", "BMSCH": 1, "STEUS": "PP01"})
            for matnr in mats:
                stock = next((r for r in self.tables["MARD"] if r["MATNR"] == matnr and r["WERKS"] == werks), None)
                if stock and stock["LABST"] > 0 and rng.random() < 0.34:
                    charg = "B" + self.counters["CHARG"].next()
                    self.add("MCH1", {"CHARG": charg, "MATNR": matnr, "ERSDA": "20240115", "VFDAT": "20261231", "HSDAT": "20240110", "LICHA": ""})
                    self.add("MCHA", {"CHARG": charg, "MATNR": matnr, "WERKS": werks, "ERSDA": "20240115", "VFDAT": "20261231", "BWTAR": ""})
                    self.add("MCHB", {"CHARG": charg, "MATNR": matnr, "WERKS": werks, "LGORT": "0001", "CLABS": stock["LABST"], "CINSM": 0, "CSPEM": 0})

    def _assets(self):
        for cc in self.spec.company_codes:
            for i in range(1, 6):
                anln1 = f"{int(cc.bukrs) * 100 + i:08d}"
                self.add("ANLA", {"BUKRS": cc.bukrs, "ANLN1": anln1, "ANLN2": "0", "TXT50": f"Machine {i} {cc.name}", "ANLKL": "2000", "KOSTL": self.cc_costcenters[cc.bukrs][0], "AKTIV": "20200101"})
                for y in self.spec.fiscal_years:
                    acq = self._amount(10000, 90000)
                    self.add("ANLC", {"BUKRS": cc.bukrs, "ANLN1": anln1, "ANLN2": "0", "GJAHR": y, "AFABE": "01", "KANSW": acq, "KNAFA": round(acq * 0.1 * (y - 2020), 2), "NAFAG": round(acq * 0.1, 2)})

    # ------------------------------------------------------------ FI helpers
    FX_RATES = {("EUR", "USD"): 1.1, ("USD", "EUR"): 0.9}  # local -> document currency, fixed for determinism

    def _post_fi(self, bukrs: str, year: int, blart: str, posting: date, lines: list[dict], awtyp: str = "", awkey: str = "", bvorg: str = "", xblnr: str = "", waers: str | None = None) -> str:
        """Post a balanced accounting document. lines: dicts with HKONT, amount (+debit/-credit, local currency),
        optional KOART/KUNNR/LIFNR/KOSTL/PRCTR/VBUND/MATNR/WERKS. `waers` posts the document in a foreign currency:
        WRBTR carries the document-currency amount at the fixed rate, DMBTR the local amount."""
        cc = self.cc_by_code[bukrs]
        belnr = self.counters["BELNR"].next()
        total = round(sum(line["amount"] for line in lines), 2)
        assert abs(total) < 0.005, f"unbalanced document {bukrs}/{belnr}: {total}"
        doc_cur = waers or cc.currency
        rate = self.FX_RATES.get((cc.currency, doc_cur), 1.0) if doc_cur != cc.currency else 1.0
        self.add("BKPF", {"BUKRS": bukrs, "BELNR": belnr, "GJAHR": year, "BLART": blart, "BLDAT": self._ds(posting), "BUDAT": self._ds(posting), "MONAT": posting.month, "WAERS": doc_cur, "AWTYP": awtyp, "AWKEY": awkey, "BVORG": bvorg, "XBLNR": xblnr, "BSTAT": ""})
        for i, line in enumerate(lines, start=1):
            amt = line["amount"]
            self.add("BSEG", {"BUKRS": bukrs, "BELNR": belnr, "GJAHR": year, "BUZEI": i, "KOART": line.get("KOART", "S"), "SHKZG": "S" if amt >= 0 else "H", "HKONT": line["HKONT"], "DMBTR": abs(amt), "WRBTR": round(abs(amt) * rate, 2), "KUNNR": line.get("KUNNR", ""), "LIFNR": line.get("LIFNR", ""), "KOSTL": line.get("KOSTL", ""), "PRCTR": line.get("PRCTR", ""), "AUGBL": line.get("AUGBL", ""), "AUGDT": line.get("AUGDT", ""), "VBUND": line.get("VBUND", ""), "MATNR": line.get("MATNR", ""), "WERKS": line.get("WERKS", "")})
            if line.get("KOART") == "D":
                self.add("BSID", {"BUKRS": bukrs, "KUNNR": line["KUNNR"], "UMSKS": "", "UMSKZ": "", "AUGDT": line.get("AUGDT", ""), "AUGBL": line.get("AUGBL", ""), "ZUONR": belnr, "GJAHR": year, "BELNR": belnr, "BUZEI": i, "DMBTR": abs(amt), "SHKZG": "S" if amt >= 0 else "H"})
            if line.get("KOART") == "K":
                self.add("BSIK", {"BUKRS": bukrs, "LIFNR": line["LIFNR"], "UMSKS": "", "UMSKZ": "", "AUGDT": line.get("AUGDT", ""), "AUGBL": line.get("AUGBL", ""), "ZUONR": belnr, "GJAHR": year, "BELNR": belnr, "BUZEI": i, "DMBTR": abs(amt), "SHKZG": "S" if amt >= 0 else "H"})
        return belnr

    def _clear(self, bukrs: str, year: int, posting: date, koart: str, partner: str, amount: float, open_belnr: str):
        """Simulate payment/clearing: posts bank document and marks the open item as cleared."""
        acct = "140000" if koart == "D" else "160000"
        sign = -1 if koart == "D" else 1
        lines = [{"HKONT": "113100", "amount": -sign * amount, "KOART": "S"},
                 {"HKONT": acct, "amount": sign * amount, "KOART": koart, ("KUNNR" if koart == "D" else "LIFNR"): partner}]
        clearing = self._post_fi(bukrs, year, "ZP" if koart == "D" else "KZ", posting, lines)
        # mark original open item as cleared
        oi_table = "BSID" if koart == "D" else "BSIK"
        for row in self.tables[oi_table]:  # the invoice item and the payment's partner item clear each other: both carry the clearing document
            if row["BUKRS"] == bukrs and row["BELNR"] in (open_belnr, clearing):
                row["AUGBL"] = clearing
                row["AUGDT"] = self._ds(posting)
        for row in self.tables["BSEG"]:
            if row["BUKRS"] == bukrs and row["BELNR"] in (open_belnr, clearing) and row["KOART"] == koart:
                row["AUGBL"] = clearing
                row["AUGDT"] = self._ds(posting)
        return clearing

    # --------------------------------------------------------------- cycles
    def _sales_cycle(self, cc: CompanyCodeSpec, year: int):
        rng = self.rng
        n = 10 * self.spec.scale
        for _ in range(n):
            kunnr = rng.choice(self.cc_customers.get(cc.bukrs) or [next(iter(self.cc_customers.values()))[0]])
            cross = rng.random() < self.spec.cross_company_sales_ratio and len(self.spec.company_codes) > 1
            if cross:
                other = rng.choice([c for c in self.spec.company_codes if c.bukrs != cc.bukrs])
                werks = rng.choice(other.plants)
            else:
                werks = rng.choice(cc.plants)
            mats = self.plant_materials.get(werks) or [self.tables["MARA"][0]["MATNR"]]
            so = self.counters["VBELN_SO"].next()
            d = self._date(year)
            n_items = rng.randint(1, 3)
            items = []
            total = 0.0
            for pos in range(1, n_items + 1):
                qty = rng.randint(1, 50)
                netwr = round(qty * self._amount(20, 400), 2)
                total += netwr
                items.append({"VBELN": so, "POSNR": pos * 10, "MATNR": rng.choice(mats), "WERKS": werks, "KWMENG": qty, "NETWR": netwr, "PRCTR": self.cc_profitcenters[cc.bukrs][0]})
            stage = rng.random()
            delivered = stage < 0.85
            billed = delivered and stage < 0.70
            cleared = billed and stage < 0.45
            self.add("VBAK", {"VBELN": so, "AUART": "OR", "VKORG": cc.bukrs, "VTWEG": "10", "SPART": "00", "KUNNR": kunnr, "AUDAT": self._ds(d), "WAERK": cc.currency, "NETWR": round(total, 2), "BUKRS_VF": cc.bukrs, "GBSTK": "C" if billed else ("B" if delivered else "A")})
            for it in items:
                self.add("VBAP", it)
            if not delivered:
                continue
            dl = self.counters["VBELN_DL"].next()
            dd = d + timedelta(days=rng.randint(1, 20))
            self.add("LIKP", {"VBELN": dl, "LFART": "LF", "VSTEL": werks, "KUNNR": kunnr, "WADAT_IST": self._ds(dd), "WERKS": werks, "BUKRS": self.plant_cc[werks]})
            for it in items:
                self.add("LIPS", {"VBELN": dl, "POSNR": it["POSNR"], "MATNR": it["MATNR"], "WERKS": werks, "LFIMG": it["KWMENG"], "VGBEL": so, "VGPOS": it["POSNR"]})
                self.add("VBFA", {"VBELV": so, "POSNV": it["POSNR"], "VBELN": dl, "POSNN": it["POSNR"], "VBTYP_N": "J", "VBTYP_V": "C", "RFMNG": it["KWMENG"]})
            # goods issue
            gi = self.counters["MBLNR"].next()
            self.add("MKPF", {"MBLNR": gi, "MJAHR": year, "BLDAT": self._ds(dd), "BUDAT": self._ds(dd), "TCODE2": "VL02N", "XBLNR": dl})
            for i, it in enumerate(items, start=1):
                self.add("MSEG", {"MBLNR": gi, "MJAHR": year, "ZEILE": i, "BWART": "601", "MATNR": it["MATNR"], "WERKS": werks, "LGORT": "0001", "MENGE": it["KWMENG"], "DMBTR": round(it["NETWR"] * 0.6, 2), "BUKRS": self.plant_cc[werks], "EBELN": "", "EBELP": "", "AUFNR": "", "UMWRK": "", "SHKZG": "H"})
                self.add("VBFA", {"VBELV": dl, "POSNV": it["POSNR"], "VBELN": gi, "POSNN": i, "VBTYP_N": "R", "VBTYP_V": "J", "RFMNG": it["KWMENG"]})
            cogs = round(sum(it["NETWR"] for it in items) * 0.6, 2)
            del_cc = self.plant_cc[werks]
            self._post_fi(del_cc, year, "WL", dd, [{"HKONT": "893000", "amount": cogs, "KOSTL": self.cc_costcenters[del_cc][1]}, {"HKONT": "310000", "amount": -cogs}], awtyp="MKPF", awkey=f"{gi}{year}")
            if not billed:
                continue
            bi = self.counters["VBELN_BI"].next()
            bd = dd + timedelta(days=rng.randint(0, 10))
            fx = "USD" if cc.currency == "EUR" else "EUR"  # every fifth invoice is billed in a foreign currency
            foreign = fx if int(bi[-1]) % 5 == 0 else None
            self.add("VBRK", {"VBELN": bi, "FKART": "F2", "VKORG": cc.bukrs, "KUNRG": kunnr, "BUKRS": cc.bukrs, "FKDAT": self._ds(bd), "WAERK": foreign or cc.currency, "NETWR": round(total * (self.FX_RATES.get((cc.currency, foreign), 1.0) if foreign else 1.0), 2), "RFBSK": "C", "GJAHR": bd.year})
            for it in items:
                self.add("VBRP", {"VBELN": bi, "POSNR": it["POSNR"], "MATNR": it["MATNR"], "WERKS": werks, "FKIMG": it["KWMENG"], "NETWR": it["NETWR"], "VGBEL": dl, "VGPOS": it["POSNR"], "AUBEL": so, "AUPOS": it["POSNR"]})
                self.add("VBFA", {"VBELV": dl, "POSNV": it["POSNR"], "VBELN": bi, "POSNN": it["POSNR"], "VBTYP_N": "M", "VBTYP_V": "J", "RFMNG": it["KWMENG"]})
            fi = self._post_fi(cc.bukrs, bd.year, "RV", bd, [{"HKONT": "140000", "amount": round(total, 2), "KOART": "D", "KUNNR": kunnr}, {"HKONT": "800000", "amount": -round(total, 2), "PRCTR": self.cc_profitcenters[cc.bukrs][0]}], awtyp="VBRK", awkey=bi, waers=foreign)
            if cross:
                # intercompany billing: delivering company code invoices the selling company code
                ic_amount = round(total * 0.8, 2)
                iv = self.counters["VBELN_BI"].next()
                ic_cust = self.cc_affiliate_customer[(del_cc, cc.bukrs)]
                ic_vend = self.cc_affiliate_vendor[(cc.bukrs, del_cc)]
                self.add("VBRK", {"VBELN": iv, "FKART": "IV", "VKORG": del_cc, "KUNRG": ic_cust, "BUKRS": del_cc, "FKDAT": self._ds(bd), "WAERK": self.cc_by_code[del_cc].currency, "NETWR": ic_amount, "RFBSK": "C", "GJAHR": bd.year})
                for it in items:
                    self.add("VBRP", {"VBELN": iv, "POSNR": it["POSNR"], "MATNR": it["MATNR"], "WERKS": werks, "FKIMG": it["KWMENG"], "NETWR": round(it["NETWR"] * 0.8, 2), "VGBEL": dl, "VGPOS": it["POSNR"], "AUBEL": so, "AUPOS": it["POSNR"]})
                    self.add("VBFA", {"VBELV": dl, "POSNV": it["POSNR"], "VBELN": iv, "POSNN": it["POSNR"], "VBTYP_N": "M", "VBTYP_V": "J", "RFMNG": it["KWMENG"]})
                bvorg = f"IC{self.counters['BVORG'].next().zfill(8)}"
                self._post_fi(del_cc, bd.year, "RV", bd, [{"HKONT": "141000", "amount": ic_amount, "KOART": "D", "KUNNR": ic_cust, "VBUND": cc.bukrs}, {"HKONT": "801000", "amount": -ic_amount, "VBUND": cc.bukrs}], awtyp="VBRK", awkey=iv, bvorg=bvorg)
                self._post_fi(cc.bukrs, bd.year, "RE", bd, [{"HKONT": "410000", "amount": ic_amount, "VBUND": del_cc, "KOSTL": self.cc_costcenters[cc.bukrs][2]}, {"HKONT": "161000", "amount": -ic_amount, "KOART": "K", "LIFNR": ic_vend, "VBUND": del_cc}], awtyp="VBRK", awkey=iv, bvorg=bvorg)
            if cleared:
                self._clear(cc.bukrs, bd.year, bd + timedelta(days=rng.randint(5, 40)), "D", kunnr, round(total, 2), fi)

    def _procurement_cycle(self, cc: CompanyCodeSpec, year: int):
        rng = self.rng
        n = 8 * self.spec.scale
        for _ in range(n):
            lifnr = rng.choice(self.cc_vendors.get(cc.bukrs) or [next(iter(self.cc_vendors.values()))[0]])
            cross = rng.random() < self.spec.cross_company_po_ratio and len(self.spec.company_codes) > 1
            werks = rng.choice(rng.choice([c for c in self.spec.company_codes if c.bukrs != cc.bukrs]).plants) if cross else rng.choice(cc.plants)
            mats = self.plant_materials.get(werks) or [self.tables["MARA"][0]["MATNR"]]
            po = self.counters["EBELN"].next()
            d = self._date(year)
            stage = rng.random()
            received = stage < 0.85
            invoiced = received and stage < 0.65
            paid = invoiced and stage < 0.40
            self.add("EKKO", {"EBELN": po, "BUKRS": cc.bukrs, "BSTYP": "F", "BSART": "NB", "LIFNR": lifnr, "EKORG": cc.bukrs, "BEDAT": self._ds(d), "WAERS": cc.currency, "GJAHR": year, "KDATB": "", "KDATE": "", "KTWRT": 0})
            contract = next((c for c in self.cc_contracts.get(cc.bukrs, []) if c["LIFNR"] == lifnr and c["open"]), None)
            items = []
            for pos in range(1, rng.randint(1, 3) + 1):
                qty = rng.randint(10, 200)
                price = self._amount(5, 150)
                release = contract is not None and pos == 1  # the first item of a PO on a vendor with an open contract releases against it
                it = {"EBELN": po, "EBELP": pos * 10, "MATNR": contract["items"][0]["MATNR"] if release else rng.choice(mats), "WERKS": werks, "MENGE": qty, "NETPR": price, "NETWR": round(qty * price, 2), "ELIKZ": "X" if received else "", "KONNR": contract["EBELN"] if release else "", "KTPNR": contract["items"][0]["EBELP"] if release else "", "LOEKZ": ""}
                self.add("EKPO", it)
                items.append(it)
            total = round(sum(i["NETWR"] for i in items), 2)
            if not received:
                continue
            gr = self.counters["MBLNR"].next()
            gd = d + timedelta(days=rng.randint(2, 30))
            self.add("MKPF", {"MBLNR": gr, "MJAHR": year, "BLDAT": self._ds(gd), "BUDAT": self._ds(gd), "TCODE2": "MIGO", "XBLNR": po})
            for i, it in enumerate(items, start=1):
                self.add("MSEG", {"MBLNR": gr, "MJAHR": year, "ZEILE": i, "BWART": "101", "MATNR": it["MATNR"], "WERKS": werks, "LGORT": "0001", "MENGE": it["MENGE"], "DMBTR": it["NETWR"], "BUKRS": self.plant_cc[werks], "EBELN": po, "EBELP": it["EBELP"], "AUFNR": "", "UMWRK": "", "SHKZG": "S"})
                self.add("EKBE", {"EBELN": po, "EBELP": it["EBELP"], "ZEKKN": "00", "VGABE": "1", "GJAHR": year, "BELNR": gr, "BUZEI": i, "BWART": "101", "MENGE": it["MENGE"], "DMBTR": it["NETWR"], "BEWTP": "E"})
            gr_cc = self.plant_cc[werks]
            self._post_fi(gr_cc, year, "WE", gd, [{"HKONT": "300000", "amount": total}, {"HKONT": "191100", "amount": -total}], awtyp="MKPF", awkey=f"{gr}{year}")
            if not invoiced:
                continue
            ir = self.counters["RBKP"].next()
            idt = gd + timedelta(days=rng.randint(1, 15))
            self.add("RBKP", {"BELNR": ir, "GJAHR": year, "BUKRS": cc.bukrs, "LIFNR": lifnr, "BLDAT": self._ds(idt), "RMWWR": total, "WAERS": cc.currency, "RBSTAT": "5"})
            for i, it in enumerate(items, start=1):
                self.add("RSEG", {"BELNR": ir, "GJAHR": year, "BUZEI": i, "EBELN": po, "EBELP": it["EBELP"], "MATNR": it["MATNR"], "WERKS": werks, "WRBTR": it["NETWR"], "MENGE": it["MENGE"]})
                self.add("EKBE", {"EBELN": po, "EBELP": it["EBELP"], "ZEKKN": "00", "VGABE": "2", "GJAHR": year, "BELNR": ir, "BUZEI": i, "BWART": "", "MENGE": it["MENGE"], "DMBTR": it["NETWR"], "BEWTP": "Q"})
            fi = self._post_fi(cc.bukrs, year, "RE", idt, [{"HKONT": "191100", "amount": total}, {"HKONT": "160000", "amount": -total, "KOART": "K", "LIFNR": lifnr}], awtyp="RMRP", awkey=f"{ir}{year}")
            if paid:
                self._clear(cc.bukrs, year, idt + timedelta(days=rng.randint(5, 45)), "K", lifnr, total, fi)

    def _contracts(self):
        """Purchase contracts per company code (one per scale step): valid over the fiscal years, every third one
        (counted across company codes) ended with its items marked deleted; release orders of the procurement cycle reference the open ones."""
        rng = self.rng
        years = sorted(self.spec.fiscal_years)
        for ci, cc in enumerate(self.spec.company_codes):
            for n in range(max(1, self.spec.scale)):
                lifnr = rng.choice(self.cc_vendors.get(cc.bukrs) or [next(iter(self.cc_vendors.values()))[0]])
                ebeln = self.counters["EBELN"].next()
                ended = (ci + n) % 3 == 2
                werks = rng.choice(cc.plants)
                mats = self.plant_materials.get(werks) or [self.tables["MARA"][0]["MATNR"]]
                items = []
                for pos in range(1, rng.randint(1, 2) + 1):
                    qty = rng.randint(500, 2000)
                    price = self._amount(5, 150)
                    it = {"EBELN": ebeln, "EBELP": pos * 10, "MATNR": rng.choice(mats), "WERKS": werks, "MENGE": qty, "NETPR": price, "NETWR": round(qty * price, 2), "ELIKZ": "", "KONNR": "", "KTPNR": "", "LOEKZ": "L" if ended else ""}
                    self.add("EKPO", it)
                    items.append(it)
                self.add("EKKO", {"EBELN": ebeln, "BUKRS": cc.bukrs, "BSTYP": "K", "BSART": "MK", "LIFNR": lifnr, "EKORG": cc.bukrs, "BEDAT": f"{years[0]}0101", "WAERS": cc.currency, "GJAHR": years[0], "KDATB": f"{years[0]}0101", "KDATE": f"{years[0]}1231" if ended else f"{years[-1] + 1}1231", "KTWRT": round(sum(i["NETWR"] for i in items), 2)})
                self.cc_contracts.setdefault(cc.bukrs, []).append({"EBELN": ebeln, "LIFNR": lifnr, "items": items, "open": not ended})

    def _scheduling_agreements(self, cc: CompanyCodeSpec, year: int):
        """Scheduling agreements (two per scale step and year) with one item and three delivery schedule lines;
        the first lines are received (goods receipt, PO history, GR/IR posting) so the agreements are open and
        fully received alike."""
        rng = self.rng
        for n in range(2 * self.spec.scale):
            lifnr = rng.choice(self.cc_vendors.get(cc.bukrs) or [next(iter(self.cc_vendors.values()))[0]])
            werks = rng.choice(cc.plants)
            mats = self.plant_materials.get(werks) or [self.tables["MARA"][0]["MATNR"]]
            sa = self.counters["EBELN"].next()
            d = date(year, 1, 1) + timedelta(days=rng.randint(0, 60))
            line_qty = rng.randint(20, 100)
            price = self._amount(5, 150)
            matnr = rng.choice(mats)
            self.add("EKKO", {"EBELN": sa, "BUKRS": cc.bukrs, "BSTYP": "L", "BSART": "LP", "LIFNR": lifnr, "EKORG": cc.bukrs, "BEDAT": self._ds(d), "WAERS": cc.currency, "GJAHR": year, "KDATB": self._ds(d), "KDATE": f"{year + 1}1231", "KTWRT": 0})
            self.add("EKPO", {"EBELN": sa, "EBELP": 10, "MATNR": matnr, "WERKS": werks, "MENGE": line_qty * 3, "NETPR": price, "NETWR": round(line_qty * 3 * price, 2), "ELIKZ": "", "KONNR": "", "KTPNR": "", "LOEKZ": ""})
            received = (3 * n + year) % 4  # 0..3 schedule lines received; all three received = closed
            for k in range(1, 4):
                due = d + timedelta(days=30 * k)
                got = k <= received
                self.add("EKET", {"EBELN": sa, "EBELP": 10, "ETENR": k, "EINDT": self._ds(due), "MENGE": line_qty, "WEMNG": line_qty if got else 0})
                if not got:
                    continue
                gr = self.counters["MBLNR"].next()
                gd = min(due + timedelta(days=rng.randint(0, 5)), date(year, 12, 28))
                value = round(line_qty * price, 2)
                self.add("MKPF", {"MBLNR": gr, "MJAHR": year, "BLDAT": self._ds(gd), "BUDAT": self._ds(gd), "TCODE2": "MIGO", "XBLNR": sa})
                self.add("MSEG", {"MBLNR": gr, "MJAHR": year, "ZEILE": 1, "BWART": "101", "MATNR": matnr, "WERKS": werks, "LGORT": "0001", "MENGE": line_qty, "DMBTR": value, "BUKRS": self.plant_cc[werks], "EBELN": sa, "EBELP": 10, "AUFNR": "", "UMWRK": "", "SHKZG": "S"})
                self.add("EKBE", {"EBELN": sa, "EBELP": 10, "ZEKKN": "00", "VGABE": "1", "GJAHR": year, "BELNR": gr, "BUZEI": 1, "BWART": "101", "MENGE": line_qty, "DMBTR": value, "BEWTP": "E"})
                self._post_fi(self.plant_cc[werks], year, "WE", gd, [{"HKONT": "300000", "amount": value}, {"HKONT": "191100", "amount": -value}], awtyp="MKPF", awkey=f"{gr}{year}")

    def _production_cycle(self, cc: CompanyCodeSpec, year: int):
        rng = self.rng
        for _ in range(3 * self.spec.scale):
            werks = rng.choice(cc.plants)
            mats = self.plant_materials.get(werks) or [self.tables["MARA"][0]["MATNR"]]
            aufnr = self.counters["AUFNR"].next()
            d = self._date(year)
            qty = rng.randint(10, 100)
            fert = rng.choice(mats)
            self.add("AUFK", {"AUFNR": aufnr, "AUART": "PP01", "KOKRS": cc.kokrs, "BUKRS": cc.bukrs, "WERKS": werks, "KOSTL": self.cc_costcenters[cc.bukrs][1], "PRCTR": self.cc_profitcenters[cc.bukrs][1]})
            self.add("AFKO", {"AUFNR": aufnr, "PLNBEZ": fert, "GAMNG": qty, "GSTRP": self._ds(d), "GLTRP": self._ds(d + timedelta(days=10)), "DWERK": werks})
            self.add("AFPO", {"AUFNR": aufnr, "POSNR": 1, "MATNR": fert, "PSMNG": qty, "WEMNG": qty if rng.random() < 0.7 else 0, "DWERK": werks})
            # component consumption
            md = self.counters["MBLNR"].next()
            self.add("MKPF", {"MBLNR": md, "MJAHR": year, "BLDAT": self._ds(d), "BUDAT": self._ds(d), "TCODE2": "MB1A", "XBLNR": aufnr})
            comp_val = 0.0
            for i in range(1, 3):
                val = self._amount(100, 2000)
                comp_val += val
                self.add("MSEG", {"MBLNR": md, "MJAHR": year, "ZEILE": i, "BWART": "261", "MATNR": rng.choice(mats), "WERKS": werks, "LGORT": "0001", "MENGE": rng.randint(1, 20), "DMBTR": val, "BUKRS": cc.bukrs, "EBELN": "", "EBELP": "", "AUFNR": aufnr, "UMWRK": "", "SHKZG": "H"})
            comp_val = round(comp_val, 2)
            self._post_fi(cc.bukrs, year, "WA", d, [{"HKONT": "400000", "amount": comp_val, "KOSTL": self.cc_costcenters[cc.bukrs][1]}, {"HKONT": "300000", "amount": -comp_val}], awtyp="MKPF", awkey=f"{md}{year}")
            self.add("AFRU", {"RUECK": self.counters["RUECK"].next(), "RMZHL": 1, "AUFNR": aufnr, "LMNGA": qty, "ISM01": round(qty * 0.5, 2), "WERKS": werks, "BUDAT": self._ds(d + timedelta(days=5))})

    def _manual_fi(self, cc: CompanyCodeSpec, year: int):
        rng = self.rng
        for _ in range(5 * self.spec.scale):
            d = self._date(year)
            amt = self._amount(500, 20000)
            kind = rng.choice(["payroll", "opex", "depr"])
            if kind == "payroll":
                lines = [{"HKONT": "420000", "amount": amt, "KOSTL": rng.choice(self.cc_costcenters[cc.bukrs])}, {"HKONT": "113100", "amount": -amt}]
            elif kind == "opex":
                lines = [{"HKONT": "470000", "amount": amt, "KOSTL": rng.choice(self.cc_costcenters[cc.bukrs])}, {"HKONT": "113100", "amount": -amt}]
            else:
                lines = [{"HKONT": "480000", "amount": amt, "KOSTL": self.cc_costcenters[cc.bukrs][0]}, {"HKONT": "220000", "amount": -amt}]
            self._post_fi(cc.bukrs, year, "SA", d, lines)
        # opening equity so that balance-sheet accounts are not all zero
        if year == self.spec.fiscal_years[0]:
            eq = self._amount(500000, 2000000)
            self._post_fi(cc.bukrs, year, "SA", date(year, 1, 1), [{"HKONT": "113100", "amount": eq}, {"HKONT": "700000", "amount": -eq}])

    def _intercompany_postings(self):
        """Manual cross-company postings (service charges) between pairs of company codes."""
        rng = self.rng
        ccs = self.spec.company_codes
        for year in self.spec.fiscal_years:
            for _ in range(2 * self.spec.scale):
                a, b = rng.sample(ccs, 2)
                amt = self._amount(1000, 30000)
                d = self._date(year)
                bvorg = f"IC{self.counters['BVORG'].next().zfill(8)}"
                self._post_fi(a.bukrs, year, "SA", d, [{"HKONT": "141000", "amount": amt, "KOART": "D", "KUNNR": self.cc_affiliate_customer[(a.bukrs, b.bukrs)], "VBUND": b.bukrs}, {"HKONT": "801000", "amount": -amt, "VBUND": b.bukrs}], bvorg=bvorg, xblnr="IC service charge")
                self._post_fi(b.bukrs, year, "SA", d, [{"HKONT": "410000", "amount": amt, "VBUND": a.bukrs, "KOSTL": self.cc_costcenters[b.bukrs][3]}, {"HKONT": "161000", "amount": -amt, "KOART": "K", "LIFNR": self.cc_affiliate_vendor[(b.bukrs, a.bukrs)], "VBUND": a.bukrs}], bvorg=bvorg, xblnr="IC service charge")

    def _cross_company_stock_transfers(self):
        rng = self.rng
        ccs = self.spec.company_codes
        for year in self.spec.fiscal_years:
            for _ in range(2 * self.spec.scale):
                a, b = rng.sample(ccs, 2)
                pa, pb = rng.choice(a.plants), rng.choice(b.plants)
                mat = rng.choice(self.plant_materials.get(pa) or [self.tables["MARA"][0]["MATNR"]])
                md = self.counters["MBLNR"].next()
                d = self._date(year)
                qty = rng.randint(1, 30)
                val = self._amount(100, 3000)
                self.add("MKPF", {"MBLNR": md, "MJAHR": year, "BLDAT": self._ds(d), "BUDAT": self._ds(d), "TCODE2": "MB1B", "XBLNR": "STO"})
                self.add("MSEG", {"MBLNR": md, "MJAHR": year, "ZEILE": 1, "BWART": "301", "MATNR": mat, "WERKS": pa, "LGORT": "0001", "MENGE": qty, "DMBTR": val, "BUKRS": a.bukrs, "EBELN": "", "EBELP": "", "AUFNR": "", "UMWRK": pb, "SHKZG": "H"})
                self.add("MSEG", {"MBLNR": md, "MJAHR": year, "ZEILE": 2, "BWART": "301", "MATNR": mat, "WERKS": pb, "LGORT": "0001", "MENGE": qty, "DMBTR": val, "BUKRS": b.bukrs, "EBELN": "", "EBELP": "", "AUFNR": "", "UMWRK": pa, "SHKZG": "S"})
                bvorg = f"IC{self.counters['BVORG'].next().zfill(8)}"
                self._post_fi(a.bukrs, year, "WA", d, [{"HKONT": "141000", "amount": val, "KOART": "D", "KUNNR": self.cc_affiliate_customer[(a.bukrs, b.bukrs)], "VBUND": b.bukrs}, {"HKONT": "300000", "amount": -val, "MATNR": mat, "WERKS": pa}], awtyp="MKPF", awkey=f"{md}{year}", bvorg=bvorg)
                self._post_fi(b.bukrs, year, "WE", d, [{"HKONT": "300000", "amount": val, "MATNR": mat, "WERKS": pb}, {"HKONT": "161000", "amount": -val, "KOART": "K", "LIFNR": self.cc_affiliate_vendor[(b.bukrs, a.bukrs)], "VBUND": a.bukrs}], awtyp="MKPF", awkey=f"{md}{year}", bvorg=bvorg)

    def _interfaces_and_jobs(self):
        self.add("RFCDES", {"RFCDEST": "PI_PROD", "RFCTYPE": "3", "RFCHOST": "pi-prod.internal", "RFCOPTIONS": "Middleware"})
        self.add("RFCDES", {"RFCDEST": "BW_PROD", "RFCTYPE": "3", "RFCHOST": "bw-prod.internal", "RFCOPTIONS": "Data warehouse extraction"})
        self.add("RFCDES", {"RFCDEST": "EDI_GATEWAY", "RFCTYPE": "T", "RFCHOST": "edi-gw.internal", "RFCOPTIONS": "EDI subsystem"})
        self.add("RFCDES", {"RFCDEST": "BANK_CONNECT", "RFCTYPE": "T", "RFCHOST": "bank.internal", "RFCOPTIONS": "Payment file transfer"})
        for cc in self.spec.company_codes:
            self.add("EDPP1", {"PARNUM": f"CUST{cc.bukrs}", "PARTYP": "KU", "RCVPOR": "EDI_GATEWAY", "MESTYP": "ORDERS"})
            self.add("TBTCO", {"JOBNAME": f"Z_PAYMENT_RUN_{cc.bukrs}", "JOBCOUNT": "01", "SDLUNAME": "BATCHFI", "STATUS": "S", "PERIODIC": "X"})
        self.add("TBTCO", {"JOBNAME": "Z_BW_DELTA_EXTRACT", "JOBCOUNT": "01", "SDLUNAME": "BATCHBW", "STATUS": "S", "PERIODIC": "X"})
        self.add("TBTCO", {"JOBNAME": "SAP_COLLECTOR_FOR_PERFMONITOR", "JOBCOUNT": "01", "SDLUNAME": "DDIC", "STATUS": "S", "PERIODIC": "X"})

    def _custom_tables(self):
        for cc in self.spec.company_codes:
            self.add("ZFI_TSA_SCOPE", {"BUKRS": cc.bukrs, "TSA_ID": "TSA-IT-01", "SERVICE": "Shared IT hosting", "END_DATE": "20271231"})
        for v in self.tables["LFA1"][: 10]:
            self.add("ZMM_SUPPLIER_EXT", {"LIFNR": v["LIFNR"], "RISK_RATING": self.rng.choice(["LOW", "MEDIUM", "HIGH"]), "ESG_SCORE": self.rng.randint(40, 95)})


def generate_landscape(spec: LandscapeSpec | None = None) -> dict[str, list[dict]]:
    return EccLandscapeGenerator(spec).generate()
