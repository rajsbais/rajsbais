"""UI end-to-end smoke (Playwright). Runs only when SDTF_E2E=1 and the UI + API are up:
   SDTF_E2E=1 SDTF_E2E_URL=http://localhost:5173 pytest tests/e2e -q
It signs in, opens all 18 applications, asserts no page errors and no failed API calls apart from
role-restricted audit endpoints, and exercises graph traversal, scope preview, the cockpit staging-file export with a registered template, and cutover risk."""
import os

import pytest

pytestmark = pytest.mark.skipif(os.getenv("SDTF_E2E") != "1", reason="set SDTF_E2E=1 with a running UI/API")
PAGES = ["/", "/portfolio", "/landscape", "/analyzer", "/org", "/catalog", "/graph", "/scope", "/carveout", "/bluefield", "/merger", "/rules", "/quality", "/runs", "/delta", "/reconciliation", "/cutover", "/copilot", "/compliance"]
ALLOWED_403 = ("/audit/events", "/audit/verify")


def test_all_screens_render_against_live_api():
    from playwright.sync_api import sync_playwright

    base = os.getenv("SDTF_E2E_URL", "http://localhost:5173")
    problems = []
    with sync_playwright() as p:
        kwargs = {"args": ["--no-sandbox"]}
        if os.getenv("SDTF_E2E_CHROME"):
            kwargs["executable_path"] = os.environ["SDTF_E2E_CHROME"]
        b = p.chromium.launch(**kwargs)
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        pg.on("pageerror", lambda e: problems.append(("pageerror", str(e))))
        pg.on("response", lambda r: problems.append(("http", r.url, r.status)) if r.status >= 400 and "/api/" in r.url and not any(a in r.url for a in ALLOWED_403) else None)
        pg.goto(base + "/")
        pg.wait_for_selector("form", state="attached")
        pg.evaluate("() => { const d = document.querySelector('details'); if (d) d.open = true; }")  # dev form is collapsed when SSO is on
        pg.fill("input >> nth=0", "architect")
        pg.fill("input[type=password]", "architect")
        pg.click("button[type=submit]")
        pg.wait_for_timeout(1500)
        for path in PAGES:
            pg.goto(base + path)
            pg.wait_for_timeout(1500)
            assert len(pg.inner_text("main")) > 150, path
        pg.goto(base + "/graph")
        pg.wait_for_timeout(1500)
        btns = pg.locator("main .row button.secondary")
        assert btns.count() > 1
        btns.nth(1).click()
        pg.wait_for_timeout(1200)
        pg.click("text=Traverse for selective extraction")
        pg.wait_for_timeout(1500)
        assert "Selective extraction set" in pg.inner_text("main")
        pg.goto(base + "/scope")
        pg.wait_for_timeout(1000)
        pg.click("text=Preview impact")
        pg.wait_for_timeout(5000)
        assert "Objects in scope" in pg.inner_text("main")
        pg.goto(base + "/runs")
        pg.wait_for_timeout(1500)
        pg.click("button:has-text('cockpit files'), button:has-text('Re-export')")
        pg.wait_for_timeout(4000)
        txt = pg.inner_text("main")
        assert "Migration object" in txt and "Migration objects of the target release" in txt and "SD.BillingDocument" in txt and "Download zip" in txt
        pg.select_option("main select >> nth=-1", "FI.GLAccount")
        pg.click("button:has-text('Use illustrative sample')")
        pg.wait_for_timeout(2500)
        txt = pg.inner_text("main")
        assert "Chart of Accounts Data" in txt and "ACCT_GROUP" in txt  # registered template with its mapping report
        pg.goto(base + "/cutover")
        pg.wait_for_timeout(1000)
        pg.click("text=Assess cutover risk")
        pg.wait_for_timeout(2500)
        assert "Go / no-go" in pg.inner_text("main")
        b.close()
    assert not problems, problems
