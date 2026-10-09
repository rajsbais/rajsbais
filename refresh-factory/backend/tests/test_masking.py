import pytest

from rfactory.masking.engine import (CATALOG, MaskingEngine, MaskingPolicy, MaskMode, Rule, TokenVault, discover_sensitive,
                                     template)
from rfactory.sap.synthetic import build_source_dataset


def engine(mode=MaskMode.PSEUDONYMIZE, key=b"k" * 32):
    pol = template("gdpr-standard")
    for r in pol.rules:
        r.mode = mode
    return MaskingEngine(pol, persistent_key=key)


def test_deterministic_and_consistent_across_tables():
    e = engine()
    src = build_source_dataset()
    kna = src["KNA1"][0]
    adrc = next(a for a in src["ADRC"] if a["ADDRNUMBER"] == kna["ADRNR"])
    assert kna["NAME1"] == adrc["NAME1"]
    m1, m2 = e.mask_row("KNA1", kna), e.mask_row("ADRC", adrc)
    assert m1["NAME1"] != kna["NAME1"] and m1["NAME1"] == m2["NAME1"]  # same person → same pseudonym in every table
    assert e.mask_row("KNA1", kna) == m1                                   # deterministic
    assert engine(key=b"z" * 32).mask_row("KNA1", kna)["NAME1"] != m1["NAME1"]  # key dependent


def test_keys_and_non_sensitive_fields_untouched():
    kna = build_source_dataset()["KNA1"][0]
    out = engine().mask_row("KNA1", kna)
    assert out["KUNNR"] == kna["KUNNR"] and out["LAND1"] == kna["LAND1"] and out["ADRNR"] == kna["ADRNR"]


def test_format_preservation():
    e = engine()
    kna = build_source_dataset()["KNA1"][0]
    out = e.mask_row("KNA1", kna)
    assert len(out["TELF1"]) == len(kna["TELF1"]) and [c.isdigit() for c in out["TELF1"]] == [c.isdigit() for c in kna["TELF1"]]
    assert out["TELF1"] != kna["TELF1"]
    assert out["STCD1"][:2] == kna["STCD1"][:2] and len(out["STCD1"]) == len(kna["STCD1"])


def test_iban_masking_yields_valid_check_digits():
    e = engine()
    kb = build_source_dataset()["KNBK"][0]
    out = e.mask_row("KNBK", kb)["IBAN"]
    assert out != kb["IBAN"] and out[:2] == kb["IBAN"][:2] and len(out) == len(kb["IBAN"])
    rearranged = out[4:] + out[:4]
    assert int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def test_empty_values_pass_through_and_are_counted():
    e = engine()
    e.mask_row("KNA1", {"KUNNR": "1", "NAME1": "", "STRAS": None})
    assert e.stats[("KNA1", "NAME1")]["empty"] == 1


def test_key_fields_cannot_be_masked():
    with pytest.raises(ValueError, match="key fields"):
        MaskingEngine(MaskingPolicy("x", "x", [Rule("KNA1", "KUNNR", "REDACT", "id")]))
    with pytest.raises(ValueError):
        MaskingEngine(MaskingPolicy("x", "x", [Rule("KNA1", "NOPE", "REDACT", "id")]))


def test_anonymize_is_irreversible_and_not_stable_across_runs():
    kna = build_source_dataset()["KNA1"][0]
    a, b = engine(MaskMode.ANONYMIZE), engine(MaskMode.ANONYMIZE)
    ma = a.mask_row("KNA1", kna)["NAME1"]
    assert ma == a.mask_row("KNA1", kna)["NAME1"]          # consistent within a run (joins survive)
    assert ma != b.mask_row("KNA1", kna)["NAME1"]          # different across runs
    a.destroy_ephemeral_key()
    with pytest.raises(RuntimeError):
        a.mask_row("KNA1", kna)


def test_protection_class_is_honest():
    assert engine(MaskMode.PSEUDONYMIZE).report()["protection_classes"] == ["pseudonymized"]
    assert engine(MaskMode.ANONYMIZE).report()["protection_classes"] == ["anonymized-best-effort"]
    r = engine(MaskMode.TOKENIZE).report()
    assert r["reversible"] and r["protection_classes"] == ["pseudonymized-reversible"]
    assert "anonymiz" not in " ".join(engine(MaskMode.PSEUDONYMIZE).report()["protection_classes"])
    assert engine(MaskMode.PSEUDONYMIZE).report()["keyed_pseudonyms_reidentifiable_with_key"] is True
    assert engine(MaskMode.ANONYMIZE).report()["keyed_pseudonyms_reidentifiable_with_key"] is False


def test_tokenize_reidentification_needs_privilege():
    vault = TokenVault()
    pol = MaskingPolicy("t", "t", [Rule("KNA1", "NAME1", "NAME", "name", MaskMode.TOKENIZE)])
    e = MaskingEngine(pol, persistent_key=b"k" * 32, vault=vault)
    kna = build_source_dataset()["KNA1"][0]
    token = e.mask_row("KNA1", kna)["NAME1"]
    with pytest.raises(PermissionError):
        vault.reidentify(token, privileged=False)
    assert vault.reidentify(token, privileged=True) == kna["NAME1"]


def test_discovery_finds_catalog_and_custom_pattern_fields():
    d = build_source_dataset()
    found = {(x["table"], x["field"]): x for x in discover_sensitive({"KNA1": d["KNA1"], "ADRC": d["ADRC"], "VBAK": d["VBAK"], "KNBK": d["KNBK"]})}
    assert found[("KNA1", "NAME1")]["source"] == "catalog"
    assert found[("KNA1", "ZZ_CONTACT_EMAIL")]["source"] == "pattern" and found[("KNA1", "ZZ_CONTACT_EMAIL")]["category"] == "email"
    assert not any(t == "VBAK" for t, _ in found), "document numbers/amounts must not be classified as PII"
    assert ("KNA1", "KUNNR") not in found


def test_coverage_reports_uncovered_fields():
    e = MaskingEngine(MaskingPolicy("p", "p", [Rule("KNA1", "NAME1", "NAME", "name")]))
    cov = e.coverage([{"table": "KNA1", "field": "NAME1"}, {"table": "KNA1", "field": "TELF1"}])
    assert not cov["complete"] and cov["missing"] == [{"table": "KNA1", "field": "TELF1"}]


def test_catalog_rules_are_all_valid_against_ddic():
    for t in ("gdpr-standard", "gdpr-strict"):
        assert template(t).validate() == []
    assert len(CATALOG) >= 15
