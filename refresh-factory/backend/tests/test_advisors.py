from rfactory.agents import advisors


def test_strategy_prefers_selective_for_small_scope():
    r = advisors.refresh_strategy(source_rows=1000, scope_rows=120, source_gb=3000, config_change_needed=False,
                                  freshness_days=7, masking_required=True)
    assert r["recommendation"] == "selective" and r["requires_human_approval"] and r["rationale"]


def test_strategy_prefers_full_refresh_when_configuration_must_change():
    r = advisors.refresh_strategy(source_rows=1000, scope_rows=300, source_gb=100, config_change_needed=True,
                                  freshness_days=30, masking_required=True)
    assert r["recommendation"] in ("full_system_refresh", "client_copy") and r["recommendation"] != "selective"


def test_strategy_rejects_full_copy_when_target_too_small():
    r = advisors.refresh_strategy(source_rows=1000, scope_rows=700, source_gb=2000, config_change_needed=False,
                                  freshness_days=30, masking_required=True, target_free_gb=100)
    assert r["scores"]["full_system_refresh"] < 0 + r["scores"]["selective"]


def test_duration_estimate_is_labelled_as_assumption():
    e = advisors.estimate_duration(bytes_total=10 * 1024 * 1024, rows_total=1000)
    assert e["model_assumption"] and "not benchmark" in e["disclaimer"] and e["seconds"] > 30


def test_masking_advice_depends_on_target_role():
    d = [{"table": "KNA1", "field": "ZZ", "strategy": "EMAIL", "category": "email", "source": "pattern", "confidence": 1.0}]
    assert advisors.masking_recommendation(d, set(), "TRN")["recommended_mode"] == "ANONYMIZE"
    assert advisors.masking_recommendation(d, set(), "QAS")["recommended_mode"] == "PSEUDONYMIZE"
    assert advisors.masking_recommendation(d, {("KNA1", "ZZ")}, "QAS")["add_rules"] == []
