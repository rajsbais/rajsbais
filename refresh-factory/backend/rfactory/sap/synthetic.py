"""SIMULATED SAP ECC systems with deterministic synthetic data.

Nothing here talks to SAP. It exists so the whole pipeline (dependency expansion, conflict
analysis, masking, load, reconciliation) can be exercised and tested end to end.
Every API response that originates here carries `simulated: true`.
"""
from __future__ import annotations

import copy
import random
from datetime import date, timedelta
from typing import Any, Callable

from .adapter import ChangeLogGap, ProductionWriteBlocked, Row, SapSystem
from .ddic import NUMBER_RANGE_OBJECTS, TABLES, key_of, key_str
from .techstate import TechState

REF_DATE = date(2026, 9, 30)

_FIRST = ["Anna", "Bernd", "Chen", "Dmitri", "Elena", "Farid", "Greta", "Hiro", "Ines", "Jonas"]
_LAST = ["Schmidt", "Okafor", "Novak", "Tanaka", "Silva", "Haddad", "Larsen", "Moreau", "Rossi", "Kowalski"]
_CITY = [("Munich", "80331", "DE"), ("Hamburg", "20095", "DE"), ("Lyon", "69001", "FR"),
         ("Austin", "73301", "US"), ("Chicago", "60601", "US"), ("Milan", "20121", "IT")]
_CORP = ["Industries", "Logistics", "Retail GmbH", "Components", "Trading Ltd", "Foods", "Systems"]


def _iban(rng: random.Random, country: str = "DE") -> str:
    bban = "".join(str(rng.randint(0, 9)) for _ in range(18))
    n = int(bban + "131400")  # DE -> 1314 + 00
    check = 98 - (n % 97)
    return f"{country}{check:02d}{bban}"


def _mat(i: int) -> str:
    return f"{i:018d}"


def _cust(i: int) -> str:
    return f"{i:010d}"


def build_source_dataset(seed: int = 42, family: str = "ECC") -> dict[str, list[Row]]:
    rng = random.Random(seed)
    d: dict[str, list[Row]] = {t: [] for t in TABLES}
    d["T001"] += [
        {"BUKRS": "1000", "BUTXT": "Alpha Manufacturing GmbH", "ORT01": "Munich", "WAERS": "EUR"},
        {"BUKRS": "2000", "BUTXT": "Beta Trading Inc", "ORT01": "Austin", "WAERS": "USD"},
    ]
    d["T001W"] += [
        {"WERKS": "1000", "NAME1": "Munich Plant", "BUKRS": "1000", "LAND1": "DE"},
        {"WERKS": "1010", "NAME1": "Hamburg DC", "BUKRS": "1000", "LAND1": "DE"},
        {"WERKS": "2000", "NAME1": "Austin Plant", "BUKRS": "2000", "LAND1": "US"},
    ]
    plants = {"1000": ["1000", "1010"], "2000": ["2000"]}

    # --- materials (1-8: CC1000, 9-12: CC2000, 3 and 4 shared) ---
    mats: dict[str, list[str]] = {"1000": [], "2000": []}
    for i in range(1, 13):
        m = _mat(100000 + i)
        d["MARA"].append({"MATNR": m, "MTART": "FERT" if i % 3 else "ROH", "MATKL": f"MG{i % 4}",
                          "MEINS": "EA", "ERSDA": (REF_DATE - timedelta(days=400 + i)).isoformat()})
        d["MAKT"].append({"MATNR": m, "SPRAS": "E", "MAKTX": f"Material {i}"})
        ccs = ["1000"] if i <= 8 else ["2000"]
        if i in (3, 4):
            ccs = ["1000", "2000"]
        for cc in ccs:
            mats[cc].append(m)
            for w in plants[cc]:
                d["MARC"].append({"MATNR": m, "WERKS": w, "DISMM": "PD"})

    # --- customers (1-8 CC1000; 3 also CC2000; 9-12 CC2000) ---
    custs: dict[str, list[str]] = {"1000": [], "2000": []}
    for i in range(1, 13):
        k = _cust(100000 + i)
        city, plz, land = rng.choice(_CITY)
        name = f"{rng.choice(_LAST)} {rng.choice(_CORP)}"
        adr = f"{900000 + i}"
        d["ADRC"].append({"ADDRNUMBER": adr, "NAME1": name, "STREET": f"{rng.randint(1, 99)} Hauptstrasse",
                          "CITY1": city, "POST_CODE1": plz,
                          "TEL_NUMBER": f"+49 89 {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}",
                          "SMTP_ADDR": f"info{i}@{name.split()[0].lower()}-corp.com"})
        d["KNA1"].append({"KUNNR": k, "NAME1": name, "ORT01": city, "PSTLZ": plz,
                          "STRAS": f"{rng.randint(1, 99)} Hauptstrasse",
                          "TELF1": f"+49 89 {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}",
                          "STCD1": f"DE{rng.randint(10**8, 10**9 - 1)}", "ADRNR": adr, "LAND1": land,
                          # custom Z field holding PII with no catalog entry: found by pattern scan
                          "ZZ_CONTACT_EMAIL": f"{rng.choice(_FIRST).lower()}.{rng.choice(_LAST).lower()}@customer-mail.com"})
        d["KNBK"].append({"KUNNR": k, "BKVID": "0001", "BANKS": "DE", "BANKL": "70070010", "BANKN": f"{rng.randint(10**9, 10**10 - 1)}",
                          "IBAN": _iban(rng), "KOINH": name})
        ccs = ["1000"] if i <= 8 else ["2000"]
        if i == 3:
            ccs = ["1000", "2000"]
        for cc in ccs:
            custs[cc].append(k)
            d["KNB1"].append({"KUNNR": k, "BUKRS": cc, "AKONT": "140000"})
            d["KNVV"].append({"KUNNR": k, "VKORG": cc, "VTWEG": "10", "SPART": "00", "KDGRP": "01"})

    # --- vendors ---
    vends: dict[str, list[str]] = {"1000": [], "2000": []}
    for i in range(1, 7):
        v = f"{200000 + i:010d}"
        cc = "1000" if i <= 4 else "2000"
        city, plz, land = rng.choice(_CITY)
        name = f"{rng.choice(_LAST)} {rng.choice(_CORP)}"
        adr = f"{950000 + i}"
        d["ADRC"].append({"ADDRNUMBER": adr, "NAME1": name, "STREET": f"{rng.randint(1, 99)} Industriestr.",
                          "CITY1": city, "POST_CODE1": plz,
                          "TEL_NUMBER": f"+49 40 {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}",
                          "SMTP_ADDR": f"sales{i}@{name.split()[0].lower()}-supply.com"})
        d["LFA1"].append({"LIFNR": v, "NAME1": name, "ORT01": city, "TELF1": f"+49 40 {rng.randint(1000, 9999)}",
                          "STCD1": f"DE{rng.randint(10**8, 10**9 - 1)}", "ADRNR": adr, "LAND1": land})
        d["LFB1"].append({"LIFNR": v, "BUKRS": cc, "AKONT": "160000"})
        d["LFBK"].append({"LIFNR": v, "BKVID": "0001", "BANKS": "DE", "BANKL": "20030000", "BANKN": f"{rng.randint(10**9, 10**10 - 1)}",
                          "IBAN": _iban(rng), "KOINH": name})
        vends[cc].append(v)

    # --- sales orders -> deliveries -> billing -> FI ---
    ccs_pool = ["1000"] * 40 + ["2000"] * 15
    rng.shuffle(ccs_pool)
    vbeln_o, vbeln_d, vbeln_b, belnr = 5000001, 80000001, 90000001, 1900000001
    cross_company_done = False
    for idx, cc in enumerate(ccs_pool):
        vb = f"{vbeln_o:010d}"; vbeln_o += 1
        age = rng.randint(0, 179)
        special = cc == "1000" and not cross_company_done and idx > 10
        if special:
            age = 20
            cross_company_done = True
        erdat = REF_DATE - timedelta(days=age)
        cust = rng.choice(custs[cc])
        items = []
        for p in range(rng.randint(1, 3)):
            mat = rng.choice(mats[cc])
            werks = rng.choice(plants[cc])
            if special and p == 0:
                werks = "2000"  # CC1000 order shipping from CC2000 plant: cross-company reference
                mat = _mat(100003)  # a material that really is maintained in plant 2000 (kept after the rng calls: data stays reproducible)
            qty = rng.randint(1, 20)
            items.append({"VBELN": vb, "POSNR": f"{(p + 1) * 10:06d}", "MATNR": mat, "WERKS": werks,
                          "KWMENG": qty, "NETWR": round(qty * rng.uniform(10, 90), 2)})
        net = round(sum(i["NETWR"] for i in items), 2)
        cur = "EUR" if cc == "1000" else "USD"
        d["VBAK"].append({"VBELN": vb, "ERDAT": erdat.isoformat(), "AUART": "OR", "VKORG": cc, "BUKRS_VF": cc,
                          "KUNNR": cust, "NETWR": net, "WAERK": cur, "ERNAM": "BATCHUSR"})
        d["VBAP"] += items
        if rng.random() < 0.8:
            dl = f"{vbeln_d:010d}"; vbeln_d += 1
            d["LIKP"].append({"VBELN": dl, "ERDAT": (erdat + timedelta(days=2)).isoformat(), "KUNNR": cust,
                              "WERKS": items[0]["WERKS"]})
            for it in items:
                d["LIPS"].append({"VBELN": dl, "POSNR": it["POSNR"], "MATNR": it["MATNR"], "WERKS": it["WERKS"],
                                  "LFIMG": it["KWMENG"], "VGBEL": vb, "VGPOS": it["POSNR"]})
                d["VBFA"].append({"VBELV": vb, "POSNV": it["POSNR"], "VBELN": dl, "POSNN": it["POSNR"], "VBTYP_N": "J"})
            if rng.random() < 0.7:
                bl = f"{vbeln_b:010d}"; vbeln_b += 1
                fkdat = erdat + timedelta(days=4)
                d["VBRK"].append({"VBELN": bl, "FKDAT": fkdat.isoformat(), "BUKRS": cc, "KUNRG": cust,
                                  "NETWR": net, "WAERK": cur})
                for it in items:
                    d["VBRP"].append({"VBELN": bl, "POSNR": it["POSNR"], "MATNR": it["MATNR"], "FKIMG": it["KWMENG"],
                                      "NETWR": it["NETWR"], "VGBEL": dl, "AUBEL": vb, "AUPOS": it["POSNR"]})
                    d["VBFA"].append({"VBELV": dl, "POSNV": it["POSNR"], "VBELN": bl, "POSNN": it["POSNR"], "VBTYP_N": "M"})
                bn = f"{belnr:010d}"; belnr += 1
                d["BKPF"].append({"BUKRS": cc, "BELNR": bn, "GJAHR": str(fkdat.year), "BLDAT": fkdat.isoformat(),
                                  "BUDAT": fkdat.isoformat(), "BLART": "RV", "AWTYP": "VBRK", "AWKEY": bl, "WAERS": cur})
                d["BSEG"] += [
                    {"BUKRS": cc, "BELNR": bn, "GJAHR": str(fkdat.year), "BUZEI": "001", "KOART": "D", "SHKZG": "S",
                     "DMBTR": net, "KUNNR": cust, "LIFNR": "", "HKONT": "140000"},
                    {"BUKRS": cc, "BELNR": bn, "GJAHR": str(fkdat.year), "BUZEI": "002", "KOART": "S", "SHKZG": "H",
                     "DMBTR": net, "KUNNR": "", "LIFNR": "", "HKONT": "800000"},
                ]

    # --- purchase orders (CC1000) ---
    for n in range(15):
        eb = f"{4500000001 + n:010d}"
        ven = rng.choice(vends["1000"])
        d["EKKO"].append({"EBELN": eb, "BUKRS": "1000", "LIFNR": ven,
                          "BEDAT": (REF_DATE - timedelta(days=rng.randint(0, 150))).isoformat(),
                          "EKORG": "1000", "BSART": "NB"})
        for p in range(rng.randint(1, 2)):
            d["EKPO"].append({"EBELN": eb, "EBELP": f"{(p + 1) * 10:05d}", "MATNR": rng.choice(mats["1000"]),
                              "WERKS": rng.choice(plants["1000"]), "MENGE": rng.randint(5, 100),
                              "NETPR": round(rng.uniform(5, 50), 2)})

    def mx(t, f):
        return max((int(r[f]) for r in d[t]), default=0)

    d["NRIV"] = [
        {"OBJECT": "SD_ORDER", "NRRANGENR": "01", "FROMNUMBER": 5000000, "TONUMBER": 5999999, "NRLEVEL": mx("VBAK", "VBELN")},
        {"OBJECT": "SD_DELIV", "NRRANGENR": "01", "FROMNUMBER": 80000000, "TONUMBER": 89999999, "NRLEVEL": mx("LIKP", "VBELN")},
        {"OBJECT": "SD_BILL", "NRRANGENR": "01", "FROMNUMBER": 90000000, "TONUMBER": 99999999, "NRLEVEL": mx("VBRK", "VBELN")},
        {"OBJECT": "MM_PO", "NRRANGENR": "01", "FROMNUMBER": 4500000000, "TONUMBER": 4599999999, "NRLEVEL": mx("EKKO", "EBELN")},
    ]
    if family == "S4":
        _add_s4(d)
    return d


def _add_s4(d: dict[str, list[Row]]) -> None:
    """S/4HANA flavour: Business Partner records for every customer/vendor and ACDOCA lines for every FI posting."""
    for r in d["KNA1"]:
        d["BUT000"].append({"PARTNER": r["KUNNR"], "BU_GROUP": "CUST", "NAME_ORG1": r["NAME1"],
                            "BU_SORT1": r["NAME1"].upper()[:20], "TYPE": "2"})
    for r in d["LFA1"]:
        d["BUT000"].append({"PARTNER": r["LIFNR"], "BU_GROUP": "VEND", "NAME_ORG1": r["NAME1"],
                            "BU_SORT1": r["NAME1"].upper()[:20], "TYPE": "2"})
    awkey = {(h["BUKRS"], h["BELNR"], h["GJAHR"]): h for h in d["BKPF"]}
    for seg in d["BSEG"]:
        h = awkey[(seg["BUKRS"], seg["BELNR"], seg["GJAHR"])]
        d["ACDOCA"].append({"RLDNR": "0L", "RBUKRS": seg["BUKRS"], "GJAHR": seg["GJAHR"], "BELNR": seg["BELNR"],
                            "DOCLN": f"{int(seg['BUZEI']):06d}", "RACCT": seg["HKONT"],
                            "HSL": seg["DMBTR"] if seg["SHKZG"] == "S" else -seg["DMBTR"],
                            "KUNNR": seg["KUNNR"], "AWTYP": h["AWTYP"], "AWREF": h["AWKEY"]})


def build_target_dataset(source: dict[str, list[Row]], seed: int = 7) -> tuple[dict[str, list[Row]], dict[str, str]]:
    """QA client prior state: plant 2000 not customized, two pre-existing customers (one identical,
    one modified) and two tester-created sales orders that collide with source document numbers."""
    rng = random.Random(seed)
    d: dict[str, list[Row]] = {t: [] for t in TABLES}
    d["T001"] = copy.deepcopy(source["T001"])
    d["T001W"] = [copy.deepcopy(r) for r in source["T001W"] if r["WERKS"] != "2000"]  # config gap on purpose
    owners: dict[str, str] = {}

    c1, c2 = _cust(100001), _cust(100002)
    pii = {"NAME1": "MASKED CORP", "STRAS": "1 Masked Street", "TELF1": "+00 000 0000", "STCD1": "XX000000000",
           "ZZ_CONTACT_EMAIL": "user000000@example.test", "NAME_ORG1": "MASKED CORP", "BU_SORT1": "MASKED CORP", "STREET": "1 Masked Street", "TEL_NUMBER": "+00 000 0000",
           "SMTP_ADDR": "user000000@example.test", "BANKN": "0000000000", "IBAN": "DE00000000000000000000", "KOINH": "MASKED CORP"}
    adr_of = {r["KUNNR"]: r["ADRNR"] for r in source["KNA1"]}
    for t in ("KNA1", "KNB1", "KNVV", "KNBK", "ADRC", "BUT000"):
        for r in source[t]:
            owner_c = r.get("KUNNR") or r.get("PARTNER") or next((k for k, a in adr_of.items() if a == r.get("ADDRNUMBER")), None)
            if owner_c in (c1, c2):
                row = {k: (pii[k] if k in pii else v) for k, v in r.items()}  # earlier refresh already masked PII
                if t == "KNA1" and owner_c == c2:
                    row["LAND1"] = "CH"  # tester changed a non-sensitive field
                d[t].append(row)

    # tester-created orders reusing source document numbers inside the 90-day window
    cutoff = (REF_DATE - timedelta(days=90)).isoformat()
    recent = sorted(r["VBELN"] for r in source["VBAK"] if r["BUKRS_VF"] == "1000" and r["ERDAT"] >= cutoff)
    clash = recent[:2] if len(recent) >= 2 else recent
    for n in clash:
        d["VBAK"].append({"VBELN": n, "ERDAT": (REF_DATE - timedelta(days=3)).isoformat(), "AUART": "OR",
                          "VKORG": "1000", "BUKRS_VF": "1000", "KUNNR": c1, "NETWR": 111.0, "WAERK": "EUR",
                          "ERNAM": "QA_ALICE"})
        d["VBAP"].append({"VBELN": n, "POSNR": "000010", "MATNR": _mat(100001), "WERKS": "1000", "KWMENG": 1, "NETWR": 111.0})
        owners[f"VBAK/{n}"] = "qa.alice"
    level = max((int(n) for n in clash), default=5000000)
    d["NRIV"] = [
        {"OBJECT": "SD_ORDER", "NRRANGENR": "01", "FROMNUMBER": 5000000, "TONUMBER": 5999999, "NRLEVEL": level},
        {"OBJECT": "SD_DELIV", "NRRANGENR": "01", "FROMNUMBER": 80000000, "TONUMBER": 89999999, "NRLEVEL": 80000000},
        {"OBJECT": "SD_BILL", "NRRANGENR": "01", "FROMNUMBER": 90000000, "TONUMBER": 99999999, "NRLEVEL": 90000000},
        {"OBJECT": "MM_PO", "NRRANGENR": "01", "FROMNUMBER": 4500000000, "TONUMBER": 4599999999, "NRLEVEL": 4500000000},
    ]
    return d, owners


class SimulatedSap:
    """In-memory SAP stand-in implementing both SourceAdapter and TargetAdapter."""

    def __init__(self, system: SapSystem, data: dict[str, list[Row]], owners: dict[str, str] | None = None,
                 outbound: list[dict] | None = None, tech: TechState | None = None):
        self.system = system
        self.tech = tech if tech is not None else TechState.default_for(system)  # simulated technical configuration
        self.data = data
        self.owners = owners or {}
        self._outbound = outbound if outbound is not None else []
        self._idx: dict[tuple[str, str], dict[Any, list[Row]]] = {}
        self.fault_injector: Callable[[str, str], None] | None = None  # (op, table) -> may raise
        self.ref_date = REF_DATE
        # change-document stand-in (CDHDR/CDPOS): every source mutation made through sim_* is logged
        self.changelog: list[dict] = []
        self.log_floor = 0  # oldest retained position; older positions raise ChangeLogGap

    # --- read side -------------------------------------------------------
    def reference_date(self) -> date:
        return self.ref_date

    # --- change documents (read side, part of SourceAdapter) ---------------
    def change_seq(self) -> int:
        return self.changelog[-1]["seq"] if self.changelog else self.log_floor

    def changes_since(self, seq: int) -> list[dict]:
        if seq < self.log_floor:
            raise ChangeLogGap(f"change log retained from {self.log_floor}, requested {seq}")
        return [dict(c) for c in self.changelog if c["seq"] > seq]

    # --- SIMULATION helpers: business users posting/changing documents in the source ---
    def _log(self, table: str, key: tuple, op: str) -> None:
        self.changelog.append({"seq": self.change_seq() + 1, "table": table, "op": op,
                               "key": dict(zip(TABLES[table].keys, key))})

    def sim_insert(self, table: str, row: Row) -> None:
        self.data[table].append(copy.deepcopy(row)); self._invalidate(table)
        self._log(table, key_of(table, row), "I")

    def sim_update(self, table: str, key: tuple, **fields) -> None:
        row = self.get(table, key)
        if row is None:
            raise KeyError(f"{table}/{key}")
        row.update(fields); self._invalidate(table)
        self._log(table, key, "U")

    def sim_delete(self, table: str, key: tuple) -> None:
        self.data[table] = [r for r in self.data[table] if key_of(table, r) != tuple(key)]
        self._invalidate(table)
        self._log(table, tuple(key), "D")

    def sim_purge_log(self) -> None:
        """Archive the change documents: positions up to now can no longer be read."""
        self.log_floor = self.change_seq(); self.changelog = []

    def _invalidate(self, table: str) -> None:
        for k in [k for k in self._idx if k[0] == table]:
            del self._idx[k]

    def select(self, table, predicate: Callable[[Row], bool] | None = None):
        return [r for r in self.data[table] if predicate is None or predicate(r)]

    def lookup(self, table, field, value):
        ix = self._idx.get((table, field))
        if ix is None:
            ix = {}
            for r in self.data[table]:
                ix.setdefault(r.get(field), []).append(r)
            self._idx[(table, field)] = ix
        return list(ix.get(value, []))

    def get(self, table, key):
        kf = TABLES[table].keys
        first = self.lookup(table, kf[0], key[0])
        for r in first:
            if tuple(r[k] for k in kf) == tuple(key):
                return r
        return None

    def count(self, table):
        return len(self.data[table])

    def table_counts(self):
        return {t: len(r) for t, r in self.data.items() if r}

    def discover(self) -> dict:
        s = self.system
        return {
            "simulated": True,
            "system": s.model_dump(mode="json"),
            "company_codes": [{"code": r["BUKRS"], "name": r["BUTXT"], "currency": r["WAERS"]} for r in self.data["T001"]],
            "plants": [{"plant": r["WERKS"], "name": r["NAME1"], "company_code": r["BUKRS"]} for r in self.data["T001W"]],
            "table_counts": self.table_counts(),
            "installed_components": [{"name": "SAP_BASIS", "release": s.release}, {"name": "SAP_APPL", "release": "617"}],
            "custom_fields": ["KNA1-ZZ_CONTACT_EMAIL"],
            "family": s.family,
            "refresh_mechanisms": ["selective-copy (simulated)"],
        }

    # --- write side ------------------------------------------------------
    def assert_writable(self) -> None:
        if not self.system.can_be_write_target:
            raise ProductionWriteBlocked(f"{self.system.label} is not an allowed write target")

    def _fault(self, op: str, table: str) -> None:
        if self.fault_injector:
            self.fault_injector(op, table)

    def upsert(self, table, rows):
        self.assert_writable()
        self._fault("upsert", table)
        kf = TABLES[table].keys
        existing = {tuple(r[k] for k in kf): i for i, r in enumerate(self.data[table])}
        for r in rows:
            k = tuple(r[f] for f in kf)
            if k in existing:
                self.data[table][existing[k]] = copy.deepcopy(r)
            else:
                self.data[table].append(copy.deepcopy(r))
                existing[k] = len(self.data[table]) - 1
        self._invalidate(table)

    def delete(self, table, key):
        self.assert_writable()
        kf = TABLES[table].keys
        self.data[table] = [r for r in self.data[table] if tuple(r[k] for k in kf) != tuple(key)]
        self._invalidate(table)

    def number_level(self, obj):
        for r in self.data["NRIV"]:
            if r["OBJECT"] == obj:
                return int(r["NRLEVEL"])
        return None

    def set_number_level(self, obj, level):
        self.assert_writable()
        for r in self.data["NRIV"]:
            if r["OBJECT"] == obj:
                r["NRLEVEL"] = int(level)
        self._invalidate("NRIV")

    def owner_of(self, table, key_s):
        return self.owners.get(f"{table}/{key_s}")

    def outbound_interfaces(self):
        return self._outbound


def make_demo_pair(family: str = "ECC") -> tuple[SimulatedSap, SimulatedSap]:
    s4 = family == "S4"
    prod, rel = ("SAP S/4HANA 2023", "758") if s4 else ("SAP ECC 6.0 EHP8", "731")
    sids = ("S4P", "S4Q") if s4 else ("EP1", "EQ1")
    src_data = build_source_dataset(family=family)
    tgt_data, owners = build_target_dataset(src_data)
    src = SimulatedSap(SapSystem(id="", sid=sids[0], client="100", role="PRD", owner="finance-it", product=prod, release=rel,
                                 app_servers=["ep1app01", "ep1app02"], tags=["synthetic"]), src_data)
    tgt = SimulatedSap(SapSystem(id="", sid=sids[1], client="200", role="QAS", owner="qa-lead", product=prod, release=rel,
                                 app_servers=["eq1app01"], tags=["synthetic"]), tgt_data, owners,
                       outbound=[{"name": "EDI_PARTNER_OUT", "type": "IDoc", "active": False},
                                 {"name": "MAIL_RELAY", "type": "SMTP", "active": False}])
    return src, tgt


def simulate_business_activity(src: SimulatedSap) -> dict:
    """A realistic slice of source activity, all logged as change documents:
    a new order for a NEW customer, a renamed customer (PII change), an order quantity change plus a removed item,
    a new purchase order and a renamed vendor. Returns what happened (keys) for demos/tests."""
    d, ref = src.data, src.reference_date().isoformat()
    s4 = bool(d.get("BUT000"))
    new_cust = "0000100099"
    src.sim_insert("ADRC", {"ADDRNUMBER": "900099", "NAME1": "Zephyr Freight GmbH", "STREET": "7 Hafenstrasse", "CITY1": "Hamburg",
                            "POST_CODE1": "20095", "TEL_NUMBER": "+49 40 5555 0199", "SMTP_ADDR": "office@zephyr-freight.com"})
    src.sim_insert("KNA1", {"KUNNR": new_cust, "NAME1": "Zephyr Freight GmbH", "ORT01": "Hamburg", "PSTLZ": "20095",
                            "STRAS": "7 Hafenstrasse", "TELF1": "+49 40 5555 0199", "STCD1": "DE555000199", "ADRNR": "900099",
                            "LAND1": "DE", "ZZ_CONTACT_EMAIL": "kai.zephyr@zephyr-freight.com"})
    src.sim_insert("KNB1", {"KUNNR": new_cust, "BUKRS": "1000", "AKONT": "140000"})
    src.sim_insert("KNVV", {"KUNNR": new_cust, "VKORG": "1000", "VTWEG": "10", "SPART": "00", "KDGRP": "01"})
    src.sim_insert("KNBK", {"KUNNR": new_cust, "BKVID": "0001", "BANKS": "DE", "BANKL": "20030000", "BANKN": "5550001990",
                            "IBAN": "DE44200300005550001990", "KOINH": "Zephyr Freight GmbH"})
    if s4:
        src.sim_insert("BUT000", {"PARTNER": new_cust, "BU_GROUP": "CUST", "NAME_ORG1": "Zephyr Freight GmbH",
                                  "BU_SORT1": "ZEPHYR FREIGHT GMBH", "TYPE": "2"})
    vb = f"{max(int(r['VBELN']) for r in d['VBAK']) + 1:010d}"
    mat = next(r["MATNR"] for r in d["MARC"] if r["WERKS"] == "1000")
    src.sim_insert("VBAK", {"VBELN": vb, "ERDAT": ref, "AUART": "OR", "VKORG": "1000", "BUKRS_VF": "1000", "KUNNR": new_cust,
                            "NETWR": 500.0, "WAERK": "EUR", "ERNAM": "BATCHUSR"})
    src.sim_insert("VBAP", {"VBELN": vb, "POSNR": "000010", "MATNR": mat, "WERKS": "1000", "KWMENG": 10, "NETWR": 500.0})
    for r in d["NRIV"]:
        if r["OBJECT"] == "SD_ORDER":
            r["NRLEVEL"] = int(vb)

    cutoff = (src.reference_date() - timedelta(days=60)).isoformat()
    inscope = sorted((r for r in d["VBAK"] if r["BUKRS_VF"] == "1000" and r["ERDAT"] >= cutoff and r["VBELN"] != vb),
                     key=lambda r: r["VBELN"], reverse=True)  # newest documents: untouched by the demo target's tester orders
    multi = next(r for r in inscope if len(src.lookup("VBAP", "VBELN", r["VBELN"])) > 1)
    items = src.lookup("VBAP", "VBELN", multi["VBELN"])
    keep, drop = items[0], items[-1]
    src.sim_delete("VBAP", (drop["VBELN"], drop["POSNR"]))
    src.sim_update("VBAP", (keep["VBELN"], keep["POSNR"]), KWMENG=keep["KWMENG"] + 5, NETWR=round(keep["NETWR"] + 100, 2))
    remaining = src.lookup("VBAP", "VBELN", multi["VBELN"])
    src.sim_update("VBAK", (multi["VBELN"],), NETWR=round(sum(r["NETWR"] for r in remaining), 2))  # header = sum of items

    renamed = next(r for r in sorted(d["KNA1"], key=lambda r: r["KUNNR"], reverse=True)
                   if any(k["KUNNR"] == r["KUNNR"] and k["BUKRS"] == "1000" for k in d["KNB1"]) and r["KUNNR"] != new_cust)
    new_name = "Meridian Components AG"
    src.sim_update("KNA1", (renamed["KUNNR"],), NAME1=new_name, TELF1="+49 89 7777 0001")
    src.sim_update("ADRC", (renamed["ADRNR"],), NAME1=new_name, TEL_NUMBER="+49 89 7777 0001")
    if s4:
        src.sim_update("BUT000", (renamed["KUNNR"],), NAME_ORG1=new_name, BU_SORT1=new_name.upper()[:20])

    eb = f"{max(int(r['EBELN']) for r in d['EKKO']) + 1:010d}"
    ven = next(r["LIFNR"] for r in d["LFB1"] if r["BUKRS"] == "1000")
    src.sim_insert("EKKO", {"EBELN": eb, "BUKRS": "1000", "LIFNR": ven, "BEDAT": ref, "EKORG": "1000", "BSART": "NB"})
    src.sim_insert("EKPO", {"EBELN": eb, "EBELP": "00010", "MATNR": mat, "WERKS": "1000", "MENGE": 40, "NETPR": 12.5})
    for r in d["NRIV"]:
        if r["OBJECT"] == "MM_PO":
            r["NRLEVEL"] = int(eb)
    vendor = next(r for r in d["LFA1"] if r["LIFNR"] == ven)
    src.sim_update("LFA1", (ven,), NAME1="Orchard Supply Partners")
    src.sim_update("ADRC", (vendor["ADRNR"],), NAME1="Orchard Supply Partners")
    if s4:
        src.sim_update("BUT000", (ven,), NAME_ORG1="Orchard Supply Partners", BU_SORT1="ORCHARD SUPPLY PARTN")
    return {"new_customer": new_cust, "new_order": vb, "changed_order": multi["VBELN"], "dropped_item": drop["POSNR"],
            "renamed_customer": renamed["KUNNR"], "new_name": new_name, "new_po": eb, "renamed_vendor": ven,
            "change_seq": src.change_seq()}
