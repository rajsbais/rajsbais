"""SIMULATED SAP ECC systems with deterministic synthetic data.

Nothing here talks to SAP. It exists so the whole pipeline (dependency expansion, conflict
analysis, masking, load, reconciliation) can be exercised and tested end to end.
Every API response that originates here carries `simulated: true`.
"""
from __future__ import annotations

import copy
import random
from datetime import date, datetime, timedelta
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

    _add_manufacturing(d, mats, plants)
    _add_flight(d)
    _add_hr(d)

    def mx(t, f):
        return max((int(r[f]) for r in d[t]), default=0)

    d["NRIV"] = [
        {"OBJECT": "SD_ORDER", "NRRANGENR": "01", "FROMNUMBER": 5000000, "TONUMBER": 5999999, "NRLEVEL": mx("VBAK", "VBELN")},
        {"OBJECT": "SD_DELIV", "NRRANGENR": "01", "FROMNUMBER": 80000000, "TONUMBER": 89999999, "NRLEVEL": mx("LIKP", "VBELN")},
        {"OBJECT": "SD_BILL", "NRRANGENR": "01", "FROMNUMBER": 90000000, "TONUMBER": 99999999, "NRLEVEL": mx("VBRK", "VBELN")},
        {"OBJECT": "MM_PO", "NRRANGENR": "01", "FROMNUMBER": 4500000000, "TONUMBER": 4599999999, "NRLEVEL": mx("EKKO", "EBELN")},
        {"OBJECT": "PP_ORDER", "NRRANGENR": "01", "FROMNUMBER": 1000000, "TONUMBER": 1999999, "NRLEVEL": mx("AUFK", "AUFNR")},
        {"OBJECT": "PP_ROUT", "NRRANGENR": "01", "FROMNUMBER": 50000000, "TONUMBER": 59999999, "NRLEVEL": mx("PLKO", "PLNNR")},
        {"OBJECT": "MM_MBLNR", "NRRANGENR": "01", "FROMNUMBER": 4900000000, "TONUMBER": 4999999999, "NRLEVEL": mx("MKPF", "MBLNR")},
    ]
    if family == "S4":
        _add_s4(d)
    return d


def _add_manufacturing(d: dict[str, list[Row]], mats: dict[str, list[str]], plants: dict[str, list[str]]) -> None:
    """BOMs, production orders and the goods movements that belong to them. Uses its own random stream, so adding it did not change any
    other synthetic data. Quantities are consistent by construction: reserved-and-issued components equal the 261 postings, and the
    received quantity equals the 101 postings (the reconciliation checks rely on this)."""
    rng = random.Random(4242)
    roh = {"1000": [_mat(100003), _mat(100006)], "2000": [_mat(100009), _mat(100012)]}  # RAW materials per company (MTART ROH)
    fert = {"1000": [_mat(100000 + i) for i in (1, 2, 4, 5, 7, 8)], "2000": [_mat(100010), _mat(100011)]}
    bom_of: dict[tuple[str, str], tuple[str, list[dict]]] = {}
    route_of: dict[tuple[str, str], tuple[str, list[dict]]] = {}
    rrng = random.Random(4343)  # routings use their own stream too: the data generated before routings existed is unchanged
    plnnr = 50000000
    stlnr = 100
    for cc, products in fert.items():
        plant = plants[cc][0]
        for prod in products:
            stlnr += 1
            sid = f"{stlnr:08d}"
            d["STKO"].append({"STLNR": sid, "MATNR": prod, "WERKS": plant, "STLAN": "1", "BMENG": 1, "DATUV": (REF_DATE - timedelta(days=500)).isoformat()})
            items = []
            for n, comp in enumerate(rng.sample(roh[cc], rng.randint(1, 2)), start=1):
                it = {"STLNR": sid, "STLKN": f"{n * 10:08d}", "IDNRK": comp, "MENGE": rng.randint(1, 4), "MEINS": "EA"}
                d["STPO"].append(it)
                items.append(it)
            bom_of[(cc, prod)] = (sid, items)
            plnnr += 1
            gid = f"{plnnr:08d}"
            d["PLKO"].append({"PLNNR": gid, "PLNAL": "01", "MATNR": prod, "WERKS": plant, "DATUV": (REF_DATE - timedelta(days=500)).isoformat()})
            ops = []
            for n, wc in enumerate(rrng.sample(["CUT01", "ASM01", "PNT01", "QC01"], rrng.randint(2, 3)), start=1):
                op = {"PLNNR": gid, "VORNR": f"{n * 10:04d}", "ARBPL": wc, "STEUS": "PP01", "LTXA1": f"Operation {n * 10} at {wc}", "VGW01": rrng.randint(1, 8)}
                d["PLPO"].append(op)
                ops.append(op)
            route_of[(cc, prod)] = (gid, ops)
    aufnr, mblnr, rueck = 1000001, 4900000001, 8000000
    for cc, count in (("1000", 14), ("2000", 4)):
        plant = plants[cc][0]
        for _ in range(count):
            order = f"{aufnr:012d}"
            aufnr += 1
            prod = rng.choice(fert[cc])
            sid, items = bom_of[(cc, prod)]
            gid, ops = route_of[(cc, prod)]
            erdat = REF_DATE - timedelta(days=rng.randint(5, 120))
            qty = rng.randint(10, 100)
            done = rng.random() < 0.6
            d["AUFK"].append({"AUFNR": order, "AUART": "PP01", "ERDAT": erdat.isoformat(), "BUKRS": cc, "WERKS": plant, "ERNAM": "BATCHUSR"})
            d["AFKO"].append({"AUFNR": order, "GAMNG": qty, "GMEIN": "EA", "GSTRP": (erdat + timedelta(days=2)).isoformat(),
                              "GLTRP": (erdat + timedelta(days=9)).isoformat(), "STLNR": sid, "PLNNR": gid})
            for op in ops:  # the order carries its own copy of the routing's operations
                d["AFVC"].append({"AUFNR": order, "VORNR": op["VORNR"], "ARBPL": op["ARBPL"], "STEUS": op["STEUS"], "LTXA1": op["LTXA1"], "VGW01": op["VGW01"]})
            d["AFPO"].append({"AUFNR": order, "POSNR": "0001", "MATNR": prod, "PSMNG": qty, "WEMNG": qty if done else 0, "WERKS": plant})
            for n, it in enumerate(items, start=1):
                need = it["MENGE"] * qty
                d["RESB"].append({"AUFNR": order, "RSPOS": f"{n:04d}", "MATNR": it["IDNRK"], "WERKS": plant, "BDMNG": need, "ENMNG": need if done else 0})
            if done:
                budat = min(erdat + timedelta(days=rng.randint(9, 20)), REF_DATE)
                for k, op in enumerate(ops, start=1):  # a confirmation per operation; the last one reports the finished quantity
                    rueck += 1
                    d["AFRU"].append({"AUFNR": order, "VORNR": op["VORNR"], "RMZHL": "00000001", "RUECK": f"{rueck:010d}",
                                      "LMNGA": qty if k == len(ops) else qty - rrng.randint(0, min(5, qty - 1)), "ISM01": round(op["VGW01"] * qty / 10, 2),
                                      "BUDAT": budat.isoformat(), "ERNAM": "BATCHUSR"})
                for lines, bwart in (([(it["IDNRK"], it["MENGE"] * qty) for it in items], "261"), ([(prod, qty)], "101")):
                    mb = f"{mblnr:010d}"
                    mblnr += 1
                    d["MKPF"].append({"MBLNR": mb, "MJAHR": str(budat.year), "BLDAT": budat.isoformat(), "BUDAT": budat.isoformat(), "USNAM": "BATCHUSR"})
                    for z, (m, q) in enumerate(lines, start=1):
                        d["MSEG"].append({"MBLNR": mb, "MJAHR": str(budat.year), "ZEILE": f"{z:04d}", "BWART": bwart, "MATNR": m, "WERKS": plant,
                                          "BUKRS": cc, "MENGE": q, "MEINS": "EA", "DMBTR": round(q * rng.uniform(2, 40), 2), "AUFNR": order})
    for n in range(3):  # stock postings that belong to no order (initial stock): isolated material documents
        mb = f"{mblnr:010d}"
        mblnr += 1
        budat = REF_DATE - timedelta(days=rng.randint(10, 100))
        d["MKPF"].append({"MBLNR": mb, "MJAHR": str(budat.year), "BLDAT": budat.isoformat(), "BUDAT": budat.isoformat(), "USNAM": "BATCHUSR"})
        d["MSEG"].append({"MBLNR": mb, "MJAHR": str(budat.year), "ZEILE": "0001", "BWART": "561", "MATNR": roh["1000"][n % 2], "WERKS": "1000", "BUKRS": "1000",
                          "MENGE": rng.randint(50, 200), "MEINS": "EA", "DMBTR": round(rng.uniform(100, 900), 2), "AUFNR": ""})


def _add_flight(d: dict[str, list[Row]]) -> None:
    """The SAP flight demo model (airlines, connections, flights, customers, bookings). Own random stream: nothing generated before it changed."""
    rng = random.Random(5151)
    carriers = [("AA", "American Airlines", "USD"), ("LH", "Lufthansa", "EUR"), ("SQ", "Singapore Airlines", "SGD"), ("UA", "United Airlines", "USD")]
    cities = [("US", "NEW YORK", "JFK"), ("DE", "FRANKFURT", "FRA"), ("SG", "SINGAPORE", "SIN"), ("US", "SAN FRANCISCO", "SFO"), ("GB", "LONDON", "LHR"), ("JP", "TOKYO", "NRT")]
    first = ["Anna", "Ben", "Chen", "Dara", "Elif", "Farid", "Gita", "Hugo", "Ines", "Jonas", "Kiri", "Luca"]
    last = ["Keller", "Okafor", "Tanaka", "Silva", "Novak", "Haddad", "Larsen", "Moreau", "Ivanov", "Costa"]
    for n in range(1, 21):
        nm = f"{rng.choice(first)} {rng.choice(last)}"
        d["SCUSTOM"].append({"ID": f"{n:08d}", "NAME": nm, "FORM": rng.choice(["Mr.", "Mrs.", "Company"]), "STREET": f"{rng.randint(1, 120)} {rng.choice(['Oak', 'Lake', 'Hill'])} Road",
                             "POSTBOX": f"PB{rng.randint(100, 999)}", "POSTCODE": f"{rng.randint(10000, 99999)}", "CITY": rng.choice(cities)[1].title(),
                             "COUNTRY": rng.choice(cities)[0], "TELEPHONE": f"+{rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(1000, 9999)}",
                             "CUSTTYPE": rng.choice(["B", "P"]), "DISCOUNT": rng.choice([0, 5, 10]), "LANGU": "E",
                             "EMAIL": f"{nm.split()[0].lower()}.{nm.split()[1].lower()}{n}@mail.example", "WEBUSER": f"WEB{n:05d}"})
    cust = [c["ID"] for c in d["SCUSTOM"]]
    book = 0
    for cid, cname, cur in carriers:
        d["SCARR"].append({"CARRID": cid, "CARRNAME": cname, "CURRCODE": cur, "URL": f"http://www.{cname.split()[0].lower()}.example"})
        for k in range(2):
            fr, to = rng.sample(cities, 2)
            conn = f"{rng.randint(100, 999):04d}"
            while any(x["CARRID"] == cid and x["CONNID"] == conn for x in d["SPFLI"]):
                conn = f"{rng.randint(100, 999):04d}"
            d["SPFLI"].append({"CARRID": cid, "CONNID": conn, "COUNTRYFR": fr[0], "CITYFROM": fr[1], "AIRPFROM": fr[2], "COUNTRYTO": to[0], "CITYTO": to[1],
                               "AIRPTO": to[2], "DEPTIME": f"{rng.randint(5, 22):02d}0000", "ARRTIME": f"{rng.randint(5, 22):02d}3000", "DISTANCE": rng.randint(500, 9000)})
            for fl in range(5):
                fd = REF_DATE + timedelta(days=rng.randint(-120, 60))
                if any(x["CARRID"] == cid and x["CONNID"] == conn and x["FLDATE"] == fd.isoformat() for x in d["SFLIGHT"]):
                    continue
                price = round(rng.uniform(150, 1500), 2)
                flight = {"CARRID": cid, "CONNID": conn, "FLDATE": fd.isoformat(), "PRICE": price, "CURRENCY": cur, "PLANETYPE": rng.choice(["747-400", "A380-800", "737-800"]),
                          "SEATSMAX": 400, "SEATSOCC": 0, "PAYMENTSUM": 0.0}
                pay = 0.0
                for _ in range(rng.randint(3, 6)):
                    book += 1
                    cancelled = rng.random() < 0.1
                    amt = round(price * rng.choice([1, 1, 1.5, 2.5]), 2)
                    cu = rng.choice(cust)
                    owner = next(c for c in d["SCUSTOM"] if c["ID"] == cu)
                    d["SBOOK"].append({"CARRID": cid, "CONNID": conn, "FLDATE": fd.isoformat(), "BOOKID": f"{book:08d}", "CUSTOMID": cu, "CUSTTYPE": owner["CUSTTYPE"],
                                       "SMOKER": "", "LUGGWEIGHT": rng.randint(0, 30), "WUNIT": "KG", "INVOICE": "", "CLASS": rng.choice(["Y", "C", "F"]),
                                       "FORCURAM": amt, "FORCURKEY": cur, "LOCCURAM": amt, "LOCCURKEY": cur, "ORDER_DATE": (fd - timedelta(days=rng.randint(5, 60))).isoformat(),
                                       "AGENCYNUM": f"{rng.randint(1, 20):08d}", "CANCELLED": "X" if cancelled else "", "PASSNAME": owner["NAME"],
                                       "PASSBIRTH": f"{rng.randint(1950, 2005)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"})
                    if not cancelled:
                        flight["SEATSOCC"] += 1
                        pay += amt
                flight["PAYMENTSUM"] = round(pay, 2)
                d["SFLIGHT"].append(flight)


def _add_hr(d: dict[str, list[Row]]) -> None:
    """Employees with the infotypes the platform models. Own random stream. Pay is consistent by construction (annual = 12 x monthly)."""
    rng = random.Random(6161)
    first = ["Marta", "Oskar", "Priya", "Tomas", "Leila", "Dmitri", "Chloe", "Ravi", "Sofia", "Kenji", "Amara", "Jonas"]
    last = ["Brandt", "Okoye", "Sharma", "Vidal", "Nasser", "Petrov", "Dubois", "Kapoor", "Rossi", "Mori", "Adeyemi", "Lindqvist"]
    sites = [("1000", "1000"), ("1000", "1010"), ("2000", "2000")]
    for n in range(1, 41):
        pernr = f"{10000000 + n:08d}"
        cc, plant = rng.choice(sites)
        begda = (REF_DATE - timedelta(days=rng.randint(400, 4000))).isoformat()
        end = "9999-12-31"
        d["PA0003"].append({"PERNR": pernr, "ABKRS": "01" if cc == "1000" else "02", "ERDAT": begda})
        d["PA0001"].append({"PERNR": pernr, "ENDDA": end, "BEGDA": begda, "BUKRS": cc, "WERKS": plant, "PERSG": "1", "ORGEH": f"{rng.randint(50000000, 50000009)}",
                            "STELL": f"{rng.randint(30000000, 30000020)}", "KOSTL": f"{rng.randint(1, 9) * 1000 + int(plant[:2])}"})
        fn, ln = rng.choice(first), rng.choice(last)
        d["PA0002"].append({"PERNR": pernr, "ENDDA": end, "BEGDA": begda, "NACHN": ln, "VORNA": fn, "GBDAT": f"{rng.randint(1955, 2003)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                            "GESCH": rng.choice(["1", "2"]), "NATIO": rng.choice(["DE", "US", "IN", "FR"]), "PERID": f"{rng.randint(10 ** 10, 10 ** 11 - 1)}"})
        for sub in ("1", "2") if rng.random() < 0.3 else ("1",):
            d["PA0006"].append({"PERNR": pernr, "SUBTY": sub, "ENDDA": end, "BEGDA": begda, "STRAS": f"{rng.randint(1, 150)} {rng.choice(['Birch', 'Cedar', 'Elm'])} Lane",
                                "ORT01": rng.choice(["Munich", "Berlin", "Dallas", "Lyon"]), "PSTLZ": f"{rng.randint(10000, 99999)}", "LAND1": rng.choice(["DE", "US", "FR"]),
                                "TELNR": f"+{rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(100000, 999999)}"})
        monthly = round(rng.uniform(2500, 9500), 2)
        d["PA0008"].append({"PERNR": pernr, "ENDDA": end, "BEGDA": begda, "TRFGR": f"E{rng.randint(1, 9)}", "BET01": monthly, "WAERS": "EUR" if cc == "1000" else "USD",
                            "ANSAL": round(monthly * 12, 2)})
        d["PA0009"].append({"PERNR": pernr, "SUBTY": "0", "ENDDA": end, "BEGDA": begda, "EMFTX": f"{fn} {ln}", "BANKL": f"{rng.randint(10000000, 99999999)}",
                            "BANKN": f"{rng.randint(10 ** 9, 10 ** 10 - 1)}"})


def _add_s4(d: dict[str, list[Row]]) -> None:
    """S/4HANA flavour: Business Partner records for every customer/vendor and ACDOCA lines for every FI posting."""
    for r in d["KNA1"]:
        d["BUT000"].append({"PARTNER": r["KUNNR"], "BU_GROUP": "CUST", "NAME_ORG1": r["NAME1"],
                            "BU_SORT1": r["NAME1"].upper()[:20], "TYPE": "2"})
    for r in d["LFA1"]:
        d["BUT000"].append({"PARTNER": r["LIFNR"], "BU_GROUP": "VEND", "NAME_ORG1": r["NAME1"],
                            "BU_SORT1": r["NAME1"].upper()[:20], "TYPE": "2"})
    hdr = {(h["MBLNR"], h["MJAHR"]): h for h in d["MKPF"]}
    for it in d["MSEG"]:  # S/4HANA keeps material documents in MATDOC; MKPF/MSEG are only compatibility views (no rows)
        h = hdr[(it["MBLNR"], it["MJAHR"])]
        d["MATDOC"].append({**{k: it[k] for k in ("MBLNR", "MJAHR", "ZEILE", "BWART", "MATNR", "WERKS", "BUKRS", "MENGE", "MEINS", "DMBTR", "AUFNR")},
                            "BLDAT": h["BLDAT"], "BUDAT": h["BUDAT"], "USNAM": h["USNAM"]})
    d["MKPF"], d["MSEG"] = [], []
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
    # a tester-created production order that reuses a source order number inside the window (a conflict only manufacturing scopes meet)
    pp_clash = sorted(r["AUFNR"] for r in source["AUFK"] if r["BUKRS"] == "1000" and r["ERDAT"] >= cutoff)[:1]
    for n in pp_clash:
        d["AUFK"].append({"AUFNR": n, "AUART": "PP01", "ERDAT": (REF_DATE - timedelta(days=2)).isoformat(), "BUKRS": "1000", "WERKS": "1000", "ERNAM": "QA_ALICE"})
        d["AFKO"].append({"AUFNR": n, "GAMNG": 5, "GMEIN": "EA", "GSTRP": REF_DATE.isoformat(), "GLTRP": REF_DATE.isoformat(), "STLNR": ""})
        d["AFPO"].append({"AUFNR": n, "POSNR": "0001", "MATNR": _mat(100001), "PSMNG": 5, "WEMNG": 0, "WERKS": "1000"})
        owners[f"AUFK/{n}"] = "qa.alice"
    level = max((int(n) for n in clash), default=5000000)
    d["NRIV"] = [
        {"OBJECT": "SD_ORDER", "NRRANGENR": "01", "FROMNUMBER": 5000000, "TONUMBER": 5999999, "NRLEVEL": level},
        {"OBJECT": "SD_DELIV", "NRRANGENR": "01", "FROMNUMBER": 80000000, "TONUMBER": 89999999, "NRLEVEL": 80000000},
        {"OBJECT": "SD_BILL", "NRRANGENR": "01", "FROMNUMBER": 90000000, "TONUMBER": 99999999, "NRLEVEL": 90000000},
        {"OBJECT": "MM_PO", "NRRANGENR": "01", "FROMNUMBER": 4500000000, "TONUMBER": 4599999999, "NRLEVEL": 4500000000},
        {"OBJECT": "PP_ORDER", "NRRANGENR": "01", "FROMNUMBER": 1000000, "TONUMBER": 1999999, "NRLEVEL": max((int(n) for n in pp_clash), default=1000000)},
        {"OBJECT": "PP_ROUT", "NRRANGENR": "01", "FROMNUMBER": 50000000, "TONUMBER": 59999999, "NRLEVEL": 50000000},
        {"OBJECT": "MM_MBLNR", "NRRANGENR": "01", "FROMNUMBER": 4900000000, "TONUMBER": 4999999999, "NRLEVEL": 4900000000},
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
        # clock of the simulated source: change documents carry the date and time they were written (UDATE/UTIME)
        self.sim_now = datetime(REF_DATE.year, REF_DATE.month, REF_DATE.day, 8, 0, 0)
        self.commit_delay = 0  # seconds between writing a change document and its commit (a long-running transaction); invisible to readers until then

    def __getstate__(self):
        """Fault injectors are test hooks (closures): never persisted."""
        d = dict(self.__dict__)
        d["fault_injector"] = None
        return d

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
    def sim_advance(self, seconds: float) -> None:
        self.sim_now = getattr(self, "sim_now", datetime(REF_DATE.year, REF_DATE.month, REF_DATE.day, 8, 0, 0)) + timedelta(seconds=seconds)

    def _log(self, table: str, key: tuple, op: str) -> None:
        now = getattr(self, "sim_now", None)
        self.changelog.append({"seq": self.change_seq() + 1, "table": table, "op": op,
                               "key": dict(zip(TABLES[table].keys, key)),
                               "ts": now.isoformat() if now else None,
                               "visible_at": (now + timedelta(seconds=getattr(self, "commit_delay", 0))).isoformat() if now else None})

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

    def select_in(self, table, field, values):
        vals = set(values)
        return [r for r in self.data[table] if r.get(field) in vals]

    def select_between(self, table, field, lo, hi):
        return [r for r in self.data[table] if lo <= r.get(field, "") <= hi]

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
