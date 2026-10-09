from collections import defaultdict

from sdtf.catalog.tables import TABLES, record_key
from sdtf.synthetic.ecc_generator import LandscapeSpec, generate_landscape


def test_generator_is_deterministic():
    a = generate_landscape(LandscapeSpec(seed=1, scale=1))
    b = generate_landscape(LandscapeSpec(seed=1, scale=1))
    assert {k: len(v) for k, v in a.items()} == {k: len(v) for k, v in b.items()}
    assert a["BKPF"][:5] == b["BKPF"][:5]
    c = generate_landscape(LandscapeSpec(seed=2, scale=1))
    assert a["BKPF"][:5] != c["BKPF"][:5]


def test_every_accounting_document_balances():
    t = generate_landscape(LandscapeSpec(seed=3))
    bal = defaultdict(float)
    for l in t["BSEG"]:
        bal[(l["BUKRS"], l["BELNR"], l["GJAHR"])] += l["DMBTR"] if l["SHKZG"] == "S" else -l["DMBTR"]
    assert all(abs(v) < 0.005 for v in bal.values())
    headers = {(h["BUKRS"], h["BELNR"], h["GJAHR"]) for h in t["BKPF"]}
    assert set(bal) == headers


def test_carve_out_patterns_present():
    t = generate_landscape(LandscapeSpec(seed=4))
    assert sum(1 for h in t["BKPF"] if h["BVORG"]) > 10, "cross-company postings"
    views = defaultdict(set)
    for r in t["KNB1"]:
        views[r["KUNNR"]].add(r["BUKRS"])
    assert any(len(v) > 1 for v in views.values()), "shared customers"
    plants = defaultdict(set)
    for r in t["MARC"]:
        plants[r["MATNR"]].add(r["WERKS"])
    assert any(len(v) > 1 for v in plants.values()), "shared materials"
    assert any(r["BWART"] == "301" for r in t["MSEG"]), "cross-plant stock transfers"
    assert any(r["FKART"] == "IV" for r in t["VBRK"]), "intercompany billing"
    assert t["ZSD_EXPORT_CTRL"], "export-controlled custom table"
    assert any(not r["AUGBL"] for r in t["BSID"]) and any(r["AUGBL"] for r in t["BSID"]), "open and cleared items"


def test_keys_unique_and_registered():
    t = generate_landscape(LandscapeSpec(seed=5))
    for table, rows in t.items():
        assert table in TABLES
        keys = [record_key(table, r) for r in rows]
        assert len(keys) == len(set(keys)), table
