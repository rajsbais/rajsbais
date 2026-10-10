"""Synthetic business-scenario generator (no source system involved).

Builds *consistent* business objects directly for the target: new customers/vendors in a reserved synthetic number
space, documents numbered from the target's own number ranges, items using materials that are really maintained in the
target's plants, balanced accounting, complete document flow. ECC and S/4HANA (Business Partner, ACDOCA) shapes.
Generated PII-looking values are fake and still pass through the masking engine like any other data.
"""
from __future__ import annotations

import random

from ..dependency.planner import Plan, PlanInstance
from ..sap.adapter import SourceAdapter
from ..sap.synthetic import _iban

CUST_BASE, VEND_BASE = 9_000_000_001, 8_000_000_001  # synthetic partner number spaces (source data uses 1xxxxx/2xxxxx)
_ADJ = ["Alder", "Birch", "Cobalt", "Dunmore", "Ember", "Fjord", "Garnet", "Harbor"]
_NOUN = ["Freight", "Components", "Foods", "Systems", "Textiles", "Tooling", "Logistics"]
_SUFFIX = ["GmbH", "AG", "Ltd", "Inc", "SA"]


class GenError(RuntimeError):
    pass


def _mx(reader: SourceAdapter, table: str, field: str, floor: int) -> int:
    return max([floor - 1] + [int(r[field]) for r in reader.select(table) if str(r[field]).isdigit()])


def _inst(t: str, key: str, rows: dict, requires=(), configs=(), origin="REQUIRED", parent=None) -> PlanInstance:
    return PlanInstance(f"{t}:{key}", t, key, origin, parent, rows, list(requires), list(configs))


def _partner(kind: str, num: int, rng: random.Random, cc: str, s4: bool, adr: int, n: int) -> tuple[str, dict]:
    nm = f"{rng.choice(_ADJ)} {rng.choice(_NOUN)} {rng.choice(_SUFFIX)}"
    key = f"{num:010d}"
    adrc = {"ADDRNUMBER": f"{adr}", "NAME1": nm, "STREET": f"{rng.randint(1, 99)} Test Street", "CITY1": "Testville",
            "POST_CODE1": f"{rng.randint(10000, 99999)}", "TEL_NUMBER": f"+49 89 {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}",
            "SMTP_ADDR": f"synthetic.{n}.{key}@example.test"}
    bank = {"BKVID": "0001", "BANKS": "DE", "BANKL": "70070010", "BANKN": f"{rng.randint(10**9, 10**10 - 1)}", "IBAN": _iban(rng), "KOINH": nm}
    if kind == "C":
        rows = {"ADRC": [adrc], "KNA1": [{"KUNNR": key, "NAME1": nm, "ORT01": "Testville", "PSTLZ": adrc["POST_CODE1"], "STRAS": adrc["STREET"],
                                          "TELF1": adrc["TEL_NUMBER"], "STCD1": f"DE{rng.randint(10**8, 10**9 - 1)}", "ADRNR": adrc["ADDRNUMBER"],
                                          "LAND1": "DE", "ZZ_CONTACT_EMAIL": f"contact.{key}@example.test"}],
                "KNB1": [{"KUNNR": key, "BUKRS": cc, "AKONT": "140000"}],
                "KNVV": [{"KUNNR": key, "VKORG": cc, "VTWEG": "10", "SPART": "00", "KDGRP": "01"}],
                "KNBK": [{"KUNNR": key, **bank}]}
        if s4:
            rows["BUT000"] = [{"PARTNER": key, "BU_GROUP": "CUST", "NAME_ORG1": nm, "BU_SORT1": nm.upper()[:20], "TYPE": "2"}]
    else:
        rows = {"ADRC": [adrc], "LFA1": [{"LIFNR": key, "NAME1": nm, "ORT01": "Testville", "TELF1": adrc["TEL_NUMBER"],
                                          "STCD1": f"DE{rng.randint(10**8, 10**9 - 1)}", "ADRNR": adrc["ADDRNUMBER"], "LAND1": "DE"}],
                "LFB1": [{"LIFNR": key, "BUKRS": cc, "AKONT": "160000"}], "LFBK": [{"LIFNR": key, **bank}]}
        if s4:
            rows["BUT000"] = [{"PARTNER": key, "BU_GROUP": "VEND", "NAME_ORG1": nm, "BU_SORT1": nm.upper()[:20], "TYPE": "2"}]
    return key, rows


def generate(template_stage: str, target: SourceAdapter, family: str, n: int, params: dict, seed: int = 1) -> tuple[Plan, list[str]]:
    """Returns (plan with fully populated instances, root instance ids). Raises GenError if the target cannot host the scenario."""
    rng, s4 = random.Random(seed), family == "S4"
    cc = params.get("company_code", "1000")
    comp = target.get("T001", (cc,))
    if comp is None:
        raise GenError(f"target has no company code {cc}")
    plants = [p["WERKS"] for p in target.select("T001W") if p["BUKRS"] == cc]
    mats = sorted((m["MATNR"], m["WERKS"]) for m in target.select("MARC") if m["WERKS"] in plants and target.get("MARA", (m["MATNR"],)))
    if template_stage in ("mfg_completed", "mfg_open") and len({m for m, _ in mats}) < 2:
        raise GenError(f"target needs at least two materials maintained in a plant of company code {cc} (a product and a component); run a master data refresh first")
    if template_stage in ("complete", "delivered_unbilled", "order_only", "po") and not mats:
        raise GenError(f"target has no materials maintained in the plants of company code {cc}; run a master data refresh first")
    cur, today = comp["WAERS"], target.reference_date()
    ref = today.isoformat()
    c_next = max(CUST_BASE, _mx(target, "KNA1", "KUNNR", CUST_BASE) + 1)
    v_next = max(VEND_BASE, _mx(target, "LFA1", "LIFNR", VEND_BASE) + 1)
    adr = max(7_000_001, _mx(target, "ADRC", "ADDRNUMBER", 7_000_001) + 1)
    o_next = (target.number_level("SD_ORDER") or 5_000_000) + 1
    d_next = (target.number_level("SD_DELIV") or 80_000_000) + 1
    b_next = (target.number_level("SD_BILL") or 90_000_000) + 1
    p_next = (target.number_level("MM_PO") or 4_500_000_000) + 1
    f_next = max(1_900_000_001, _mx(target, "BKPF", "BELNR", 1_900_000_001) + 1)
    ao_next = (target.number_level("PP_ORDER") or 1_000_000) + 1
    mb_next = (target.number_level("MM_MBLNR") or 4_900_000_000) + 1
    pl_next = (target.number_level("PP_ROUT") or 50_000_000) + 1
    st_next = max(90_000_001, _mx(target, "STKO", "STLNR", 90_000_001) + 1)  # synthetic BOM numbers live in their own space
    matdoc = s4 or bool(target.select("MATDOC"))
    inst: dict[str, PlanInstance] = {}
    order: list[str] = []
    roots: list[str] = []
    cfg = {"COMPANY_CODE": {cc}, "PLANT": set()}

    def add(i: PlanInstance):
        inst[i.id] = i; order.append(i.id)
        for c in i.configs:
            t, code = c.split(":", 1); cfg[t].add(code)

    for n_i in range(n):
        if template_stage in ("customer", "vendor"):
            kind = "C" if template_stage == "customer" else "V"
            num = c_next if kind == "C" else v_next
            key, rows = _partner(kind, num, rng, cc, s4, adr, n_i)
            c_next += kind == "C"; v_next += kind == "V"; adr += 1
            t = "CUSTOMER" if kind == "C" else "VENDOR"
            add(_inst(t, key, rows, configs=[f"COMPANY_CODE:{cc}"], origin="ROOT")); roots.append(f"{t}:{key}")
            continue
        if template_stage == "po":
            vkey, vrows = _partner("V", v_next, rng, cc, s4, adr, n_i); v_next += 1; adr += 1
            add(_inst("VENDOR", vkey, vrows, configs=[f"COMPANY_CODE:{cc}"]))
            while target.get("EKKO", (f"{p_next:010d}",)):
                p_next += 1
            eb = f"{p_next:010d}"; p_next += 1
            items, req = [], [f"VENDOR:{vkey}"]
            for p in range(rng.randint(1, 2)):
                m, w = rng.choice(mats)
                items.append({"EBELN": eb, "EBELP": f"{(p + 1) * 10:05d}", "MATNR": m, "WERKS": w, "MENGE": rng.randint(5, 100),
                              "NETPR": round(rng.uniform(5, 50), 2)}); req.append(f"MATERIAL:{m}")
            add(_inst("PURCHASE_ORDER", eb, {"EKKO": [{"EBELN": eb, "BUKRS": cc, "LIFNR": vkey, "BEDAT": ref, "EKORG": cc, "BSART": "NB"}],
                                              "EKPO": items}, req, [f"COMPANY_CODE:{cc}"] + [f"PLANT:{i['WERKS']}" for i in items], "ROOT", f"VENDOR:{vkey}"))
            roots.append(f"PURCHASE_ORDER:{eb}")
            continue
        if template_stage in ("mfg_completed", "mfg_open"):
            plant = rng.choice(sorted({w for _, w in mats}))
            here = sorted({m for m, w in mats if w == plant})
            if len(here) < 2:
                raise GenError(f"plant {plant} has fewer than two materials (a product and a component)")
            prod, comps = here[0], rng.sample(here[1:], min(len(here) - 1, rng.randint(1, 2)))
            sid = f"{st_next:08d}"; st_next += 1
            stpo = [{"STLNR": sid, "STLKN": f"{k * 10:08d}", "IDNRK": c, "MENGE": rng.randint(1, 4), "MEINS": "EA"} for k, c in enumerate(comps, start=1)]
            add(_inst("BOM", sid, {"STKO": [{"STLNR": sid, "MATNR": prod, "WERKS": plant, "STLAN": "1", "BMENG": 1, "DATUV": ref}], "STPO": stpo},
                      [f"MATERIAL:{prod}"] + [f"MATERIAL:{c}" for c in comps], [f"PLANT:{plant}"]))
            while target.get("PLKO", (f"{pl_next:08d}",)):
                pl_next += 1
            gid = f"{pl_next:08d}"; pl_next += 1
            ops = [{"PLNNR": gid, "VORNR": f"{k * 10:04d}", "ARBPL": wc, "STEUS": "PP01", "LTXA1": f"Operation {k * 10} at {wc}", "VGW01": rng.randint(1, 8)}
                   for k, wc in enumerate(rng.sample(["CUT01", "ASM01", "PNT01", "QC01"], rng.randint(2, 3)), start=1)]
            add(_inst("ROUTING", gid, {"PLKO": [{"PLNNR": gid, "PLNAL": "01", "MATNR": prod, "WERKS": plant, "DATUV": ref}], "PLPO": ops},
                      [f"MATERIAL:{prod}"], [f"PLANT:{plant}"]))
            while target.get("AUFK", (f"{ao_next:012d}",)):
                ao_next += 1
            order_no = f"{ao_next:012d}"; ao_next += 1
            qty, done = rng.randint(10, 100), template_stage == "mfg_completed"
            resb = [{"AUFNR": order_no, "RSPOS": f"{k:04d}", "MATNR": b["IDNRK"], "WERKS": plant, "BDMNG": b["MENGE"] * qty, "ENMNG": b["MENGE"] * qty if done else 0}
                    for k, b in enumerate(stpo, start=1)]
            req = [f"BOM:{sid}", f"ROUTING:{gid}", f"MATERIAL:{prod}"] + [f"MATERIAL:{c}" for c in comps]
            afvc = [{"AUFNR": order_no, "VORNR": o["VORNR"], "ARBPL": o["ARBPL"], "STEUS": o["STEUS"], "LTXA1": o["LTXA1"], "VGW01": o["VGW01"]} for o in ops]
            afru = [{"AUFNR": order_no, "VORNR": o["VORNR"], "RMZHL": "00000001", "RUECK": f"{9_000_000 + int(order_no) % 1_000_000 * 10 + k:010d}",
                     "LMNGA": qty, "ISM01": round(o["VGW01"] * qty / 10, 2), "BUDAT": ref, "ERNAM": "TDM_SYNTH"} for k, o in enumerate(ops, start=1)] if done else []
            add(_inst("PRODUCTION_ORDER", order_no, {
                "AUFK": [{"AUFNR": order_no, "AUART": "PP01", "ERDAT": ref, "BUKRS": cc, "WERKS": plant, "ERNAM": "TDM_SYNTH"}],
                "AFKO": [{"AUFNR": order_no, "GAMNG": qty, "GMEIN": "EA", "GSTRP": ref, "GLTRP": ref, "STLNR": sid, "PLNNR": gid}],
                "AFPO": [{"AUFNR": order_no, "POSNR": "0001", "MATNR": prod, "PSMNG": qty, "WEMNG": qty if done else 0, "WERKS": plant}],
                "RESB": resb, "AFVC": afvc, "AFRU": afru}, req, [f"COMPANY_CODE:{cc}", f"PLANT:{plant}"], "ROOT", f"BOM:{sid}"))
            roots.append(f"PRODUCTION_ORDER:{order_no}")
            if not done:
                continue
            for lines, bwart in (([(b["IDNRK"], b["MENGE"] * qty) for b in stpo], "261"), ([(prod, qty)], "101")):
                while target.get("MKPF", (f"{mb_next:010d}", yr := str(today.year))) or target.lookup("MATDOC", "MBLNR", f"{mb_next:010d}"):
                    mb_next += 1
                mb = f"{mb_next:010d}"; mb_next += 1
                items = [{"MBLNR": mb, "MJAHR": yr, "ZEILE": f"{z:04d}", "BWART": bwart, "MATNR": m, "WERKS": plant, "BUKRS": cc, "MENGE": q, "MEINS": "EA",
                          "DMBTR": round(q * rng.uniform(2, 40), 2), "AUFNR": order_no} for z, (m, q) in enumerate(lines, start=1)]
                mreq = [f"PRODUCTION_ORDER:{order_no}"] + [f"MATERIAL:{m}" for m, _ in lines]
                if matdoc:  # S/4HANA: every line of MATDOC is its own instance (see registry)
                    for it in items:
                        add(_inst("MATERIAL_DOCUMENT", f"{mb}/{yr}/{it['ZEILE']}", {"MATDOC": [{**it, "BLDAT": ref, "BUDAT": ref, "USNAM": "TDM_SYNTH"}]}, mreq,
                                  [f"PLANT:{plant}", f"COMPANY_CODE:{cc}"], "DOWNSTREAM", f"PRODUCTION_ORDER:{order_no}"))
                else:
                    add(_inst("MATERIAL_DOCUMENT", f"{mb}/{yr}", {"MKPF": [{"MBLNR": mb, "MJAHR": yr, "BLDAT": ref, "BUDAT": ref, "USNAM": "TDM_SYNTH"}], "MSEG": items},
                              mreq, [f"PLANT:{plant}", f"COMPANY_CODE:{cc}"], "DOWNSTREAM", f"PRODUCTION_ORDER:{order_no}"))
            continue
        # order-to-cash family
        ckey, crows = _partner("C", c_next, rng, cc, s4, adr, n_i); c_next += 1; adr += 1
        add(_inst("CUSTOMER", ckey, crows, configs=[f"COMPANY_CODE:{cc}"]))
        while target.get("VBAK", (f"{o_next:010d}",)):
            o_next += 1
        vb = f"{o_next:010d}"; o_next += 1
        items, req = [], [f"CUSTOMER:{ckey}"]
        for p in range(rng.randint(1, 3)):
            m, w = rng.choice(mats)
            q = rng.randint(1, 20)
            items.append({"VBELN": vb, "POSNR": f"{(p + 1) * 10:06d}", "MATNR": m, "WERKS": w, "KWMENG": q, "NETWR": round(q * rng.uniform(10, 90), 2)})
            req.append(f"MATERIAL:{m}")
        net = round(sum(i["NETWR"] for i in items), 2)
        add(_inst("SALES_ORDER", vb, {"VBAK": [{"VBELN": vb, "ERDAT": ref, "AUART": "OR", "VKORG": cc, "BUKRS_VF": cc, "KUNNR": ckey,
                                                  "NETWR": net, "WAERK": cur, "ERNAM": "TDM_SYNTH"}], "VBAP": items},
                  req, [f"COMPANY_CODE:{cc}"] + [f"PLANT:{i['WERKS']}" for i in items], "ROOT", f"CUSTOMER:{ckey}"))
        roots.append(f"SALES_ORDER:{vb}")
        if template_stage == "order_only":
            continue
        while target.get("LIKP", (f"{d_next:010d}",)):
            d_next += 1
        dl = f"{d_next:010d}"; d_next += 1
        add(_inst("DELIVERY", dl, {"LIKP": [{"VBELN": dl, "ERDAT": ref, "KUNNR": ckey, "WERKS": items[0]["WERKS"]}],
                                   "LIPS": [{"VBELN": dl, "POSNR": i["POSNR"], "MATNR": i["MATNR"], "WERKS": i["WERKS"], "LFIMG": i["KWMENG"],
                                             "VGBEL": vb, "VGPOS": i["POSNR"]} for i in items],
                                   "VBFA": [{"VBELV": vb, "POSNV": i["POSNR"], "VBELN": dl, "POSNN": i["POSNR"], "VBTYP_N": "J"} for i in items]},
                  [f"SALES_ORDER:{vb}", f"CUSTOMER:{ckey}"] + [f"MATERIAL:{i['MATNR']}" for i in items],
                  [f"PLANT:{i['WERKS']}" for i in items], "DOWNSTREAM", f"SALES_ORDER:{vb}"))
        if template_stage == "delivered_unbilled":
            continue
        while target.get("VBRK", (f"{b_next:010d}",)):
            b_next += 1
        bl = f"{b_next:010d}"; b_next += 1
        add(_inst("BILLING", bl, {"VBRK": [{"VBELN": bl, "FKDAT": ref, "BUKRS": cc, "KUNRG": ckey, "NETWR": net, "WAERK": cur}],
                                  "VBRP": [{"VBELN": bl, "POSNR": i["POSNR"], "MATNR": i["MATNR"], "FKIMG": i["KWMENG"], "NETWR": i["NETWR"],
                                            "VGBEL": dl, "AUBEL": vb, "AUPOS": i["POSNR"]} for i in items],
                                  "VBFA": [{"VBELV": dl, "POSNV": i["POSNR"], "VBELN": bl, "POSNN": i["POSNR"], "VBTYP_N": "M"} for i in items]},
                  [f"DELIVERY:{dl}", f"SALES_ORDER:{vb}", f"CUSTOMER:{ckey}"] + [f"MATERIAL:{i['MATNR']}" for i in items],
                  [f"COMPANY_CODE:{cc}"], "DOWNSTREAM", f"DELIVERY:{dl}"))
        bn, yr = f"{f_next:010d}", str(today.year); f_next += 1
        fi_rows = {"BKPF": [{"BUKRS": cc, "BELNR": bn, "GJAHR": yr, "BLDAT": ref, "BUDAT": ref, "BLART": "RV", "AWTYP": "VBRK", "AWKEY": bl, "WAERS": cur}],
                   "BSEG": [{"BUKRS": cc, "BELNR": bn, "GJAHR": yr, "BUZEI": "001", "KOART": "D", "SHKZG": "S", "DMBTR": net, "KUNNR": ckey, "LIFNR": "", "HKONT": "140000"},
                            {"BUKRS": cc, "BELNR": bn, "GJAHR": yr, "BUZEI": "002", "KOART": "S", "SHKZG": "H", "DMBTR": net, "KUNNR": "", "LIFNR": "", "HKONT": "800000"}]}
        if s4:
            fi_rows["ACDOCA"] = [{"RLDNR": "0L", "RBUKRS": cc, "GJAHR": yr, "BELNR": bn, "DOCLN": f"{k + 1:06d}", "RACCT": s["HKONT"],
                                  "HSL": s["DMBTR"] if s["SHKZG"] == "S" else -s["DMBTR"], "KUNNR": s["KUNNR"], "AWTYP": "VBRK", "AWREF": bl}
                                 for k, s in enumerate(fi_rows["BSEG"])]
        add(_inst("FI_DOCUMENT", f"{cc}/{bn}/{yr}", fi_rows, [f"BILLING:{bl}", f"CUSTOMER:{ckey}"], [f"COMPANY_CODE:{cc}"], "DOWNSTREAM", f"BILLING:{bl}"))
    return Plan("synthetic", inst, order, cfg, [], []), roots
