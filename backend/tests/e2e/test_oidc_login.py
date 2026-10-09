"""Browser end-to-end test of the OIDC authorization code + PKCE login (Playwright). Opt-in:

    python -m sdtf.cli fake-idp --port 9400 &
    SDTF_OIDC_ISSUER=http://127.0.0.1:9400 SDTF_OIDC_JWKS_URL=http://127.0.0.1:9400/certs SDTF_OIDC_AUDIENCE=sdtf-ui \\
      SDTF_OIDC_ROLE_MAP='{"SAP-Migration-Architects":"architect","SAP-Business-Approvers":"approver"}' python -m sdtf.cli serve &
    (cd frontend && npm run dev) &
    SDTF_E2E=1 SDTF_E2E_OIDC=1 pytest tests/e2e/test_oidc_login.py -q

Covers: SSO button rendered from /auth/oidc/config, redirect to the provider with S256 challenge + state + nonce,
consent page, redirect back to /auth/callback, API-side code exchange, session with directory-group roles,
authenticated API calls, RP-initiated logout, and rejection of a forged callback (state mismatch)."""
import os

import pytest

pytestmark = pytest.mark.skipif(os.getenv("SDTF_E2E") != "1" or os.getenv("SDTF_E2E_OIDC") != "1", reason="set SDTF_E2E=1 SDTF_E2E_OIDC=1 with UI, API (OIDC configured) and fake IdP running")


def test_pkce_login_logout_in_browser():
    from playwright.sync_api import sync_playwright

    base = os.getenv("SDTF_E2E_URL", "http://localhost:5173")
    idp = os.getenv("SDTF_E2E_IDP", "http://127.0.0.1:9400")
    with sync_playwright() as p:
        kwargs = {"args": ["--no-sandbox"]}
        if os.getenv("SDTF_E2E_CHROME"):
            kwargs["executable_path"] = os.environ["SDTF_E2E_CHROME"]
        b = p.chromium.launch(**kwargs)
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        token_calls = []
        pg.on("request", lambda r: token_calls.append(r.url) if r.url.endswith("/token") else None)

        # 1. login page offers SSO and sends the browser to the provider with a PKCE S256 challenge
        pg.goto(base + "/runs")
        pg.wait_for_selector("text=Sign in with single sign-on")
        pg.click("text=Sign in with single sign-on")
        pg.wait_for_url(f"{idp}/auth?*")
        from urllib.parse import parse_qs, urlparse

        q = {k: v[0] for k, v in parse_qs(urlparse(pg.url).query).items()}
        assert q["response_type"] == "code" and q["client_id"] == "sdtf-ui" and q["code_challenge_method"] == "S256"
        assert len(q["code_challenge"]) == 43 and q["state"] and q["nonce"] and q["redirect_uri"] == base + "/auth/callback"
        assert "code_verifier" not in pg.url

        # 2. consent page -> redirect back -> API-side exchange -> session established with mapped roles
        pg.fill("input[name=username]", "jane.doe")
        pg.fill("input[name=name]", "Jane Doe")
        pg.check("input[value=SAP-Business-Approvers]")
        pg.click("button[type=submit]")
        pg.wait_for_selector("text=Jane Doe (architect, approver) · SSO", timeout=15000)
        assert pg.url.rstrip("/") == (base + "/runs").rstrip("/"), pg.url  # returned to the page originally requested
        assert not token_calls, "the browser must never call the token endpoint when SDTF_OIDC_EXCHANGE=api"
        me = pg.evaluate("""async () => { const r = await fetch('/api/v1/auth/me', {headers: {Authorization: 'Bearer ' + localStorage.getItem('sdtf.token')}}); return [r.status, await r.json()]; }""")
        assert me[0] == 200 and me[1]["username"] == "jane.doe" and me[1]["roles"] == ["architect", "approver"], me
        assert pg.evaluate("() => sessionStorage.getItem('sdtf.oidc.pending')") is None  # verifier/state cleared after use
        pg.goto(base + "/portfolio")
        pg.wait_for_timeout(1500)
        assert len(pg.inner_text("main")) > 100

        # 3. RP-initiated logout goes through the provider's end_session endpoint and lands on the login page
        pg.click("text=Sign out")
        pg.wait_for_selector("text=Sign in with single sign-on", timeout=15000)
        assert pg.evaluate("() => localStorage.getItem('sdtf.token')") is None
        import httpx

        assert httpx.get(idp + "/").json()["logouts"] >= 1

        # 4. a forged callback (no pending login / wrong state) is rejected
        pg.goto(base + "/auth/callback?code=forged&state=nope")
        pg.wait_for_selector("text=Sign-in failed")
        assert "no login in progress" in pg.inner_text("body")
        assert pg.evaluate("() => localStorage.getItem('sdtf.token')") is None
        b.close()
    assert not errors, errors
