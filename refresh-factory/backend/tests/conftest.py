from datetime import timedelta

import pytest

from rfactory.security.auth import DEMO_USERS as U
from rfactory.selective.manifest import Scope
from rfactory.service import RefreshService

ALICE, BOB, CAROL, ADMIN = U["alice.basis"], U["bob.steward"], U["carol.approver"], U["root.admin"]


@pytest.fixture
def svc(tmp_path):
    s = RefreshService(tmp_path)
    b = s.bootstrap_demo(ADMIN)
    s.src_id, s.tgt_id = b["source"]["id"], b["target"]["id"]
    s.s4_src, s.s4_tgt = b["s4_source"]["id"], b["s4_target"]["id"]
    return s


def make_project(svc, policy=None, src=None, tgt=None, downstream=("DELIVERY", "BILLING", "FI_DOCUMENT"), company="1000", days=90,
                 masking="gdpr-standard", object_type="SALES_ORDER", **scope_kw):
    p = svc.create_project(ALICE, "test", src or svc.src_id, tgt or svc.tgt_id)
    ref = svc.adapters[p.source_id].reference_date()
    scope = Scope(object_type=object_type, company_codes=[company] if company else [],
                  date_from=ref - timedelta(days=days) if days else None, date_to=ref if days else None, **scope_kw)
    svc.set_manifest(ALICE, p.id, scope, list(downstream), masking, policy or {})
    return p


def ready_project(svc, policy=None, **kw):
    """Plan + conflict analysis + masking coverage done; not yet submitted."""
    p = make_project(svc, {"DUPLICATE_DIFFERENT": "SKIP", "DUPLICATE_IDENTICAL": "SKIP", **(policy or {})}, **kw)
    svc.build_plan(ALICE, p.id)
    adv = svc.masking_advice(p.id)
    if adv["add_rules"]:
        svc.add_masking_rules(ALICE, p.id, [{k: r[k] for k in ("table", "field", "strategy", "category")} for r in adv["add_rules"]])
    svc.analyze_conflicts(ALICE, p.id)
    return p


def approved_project(svc, **kw):
    p = ready_project(svc, **kw)
    svc.submit(ALICE, p.id)
    svc.approve(CAROL, p.id)
    return p


def norm(data):
    """Order-insensitive view of table contents (SAP tables have no guaranteed physical order)."""
    import json
    return {t: sorted(json.dumps(r, sort_keys=True, default=str) for r in rows) for t, rows in data.items()}
