import { expect, expectAccessible, test } from "./fixtures";

test("document structure: language, one h1 per view that follows navigation, landmarks, title", async ({ app }) => {
  const { page } = app;
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  for (const v of ["Control tower", "Landscape", "Orchestration", "AI agents"]) {
    await app.nav(v);
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(v);
    await expect(page).toHaveTitle(`${v} · Keystone`);
  }
  await expect(page.getByRole("main")).toHaveCount(1);
  await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(1);
  await expect(page.getByRole("complementary")).toHaveCount(1);
});

test("headings never skip a level", async ({ app }) => {
  await app.loadLandscape();
  for (const v of ["Control tower", "Landscape", "Selective designer", "Full refresh", "AI agents", "Orchestration"]) {
    await app.nav(v);
    const levels = await app.page.locator("h1, h2, h3, h4, h5, h6").evaluateAll((hs) => hs.map((h) => Number(h.tagName[1])));
    levels.reduce((prev, l) => { expect(l - prev, `${v}: h${prev} followed by h${l}`).toBeLessThanOrEqual(1); return l; }, 0);
  }
});

test("skip link is the first tab stop and moves focus to the main content", async ({ app }) => {
  const { page } = app;
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to main content" });
  await expect(skip).toBeFocused();
  await expect(skip).toBeInViewport();
  await page.keyboard.press("Enter");
  await expect(page.locator("main")).toBeFocused();
});

test("navigation is operable by keyboard and exposes the current page", async ({ app }) => {
  const { page } = app;
  const nav = page.getByRole("navigation", { name: "Main" });
  await nav.getByRole("button", { name: "Landscape", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(nav.getByRole("button", { name: "Landscape", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(nav.locator("[aria-current=page]")).toHaveCount(1);
  await page.keyboard.press("Tab");
  await page.keyboard.press("Space");
  await expect(nav.locator("[aria-current=page]")).not.toHaveText("Landscape");
  // a view the user may not open is disabled (not focusable) and says why
  const audit = nav.getByRole("button", { name: "Audit", exact: true });
  await expect(audit).toBeDisabled();
  await expect(audit).toHaveAttribute("title", /requires audit:read/);
});

test("design-step tabs follow the WAI-ARIA tabs pattern", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Selective designer");
  const tabs = page.getByRole("tab");
  await expect(page.getByRole("tablist", { name: "Refresh design steps" })).toBeVisible();
  await expect(tabs.first()).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("[role=tab][tabindex='0']")).toHaveCount(1); // roving tabindex: one tab stop
  await tabs.first().focus();
  await page.keyboard.press("ArrowRight");
  await expect(tabs.nth(1)).toBeFocused();
  await expect(tabs.nth(1)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await expect(tabs.last()).toBeFocused();
  await page.keyboard.press("ArrowRight"); // wraps
  await expect(tabs.first()).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(tabs.last()).toBeFocused();
  await page.keyboard.press("Home");
  await expect(tabs.first()).toBeFocused();
  const panel = page.getByRole("tabpanel");
  await expect(panel).toHaveAttribute("aria-labelledby", await tabs.first().getAttribute("id") ?? "");
  await expect(tabs.first()).toHaveAttribute("aria-controls", await panel.getAttribute("id") ?? "");
  await expectAccessible(page, "designer tabs");
});

test("tables sort from the keyboard and announce the sort order", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Landscape");
  const header = page.getByRole("columnheader").first();
  const sortBtn = header.getByRole("button");
  await sortBtn.focus();
  await expect(header).toHaveAttribute("aria-sort", "none");
  await page.keyboard.press("Enter");
  await expect(header).toHaveAttribute("aria-sort", "ascending");
  await page.keyboard.press("Space");
  await expect(header).toHaveAttribute("aria-sort", "descending");
  // the scrollable table body can be reached and scrolled by keyboard users
  await expect(page.getByRole("region").first()).toHaveAttribute("tabindex", "0");
  // icon-only/empty header cells still have a name for screen readers
  for (const th of await page.getByRole("columnheader").all()) expect((await th.innerText()).trim().length + (await th.locator(".sr-only").count())).toBeGreaterThan(0);
});

test("keyboard focus is always visible", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Orchestration");
  for (const el of [page.getByRole("button", { name: "Run scheduler tick" }), page.getByLabel("Window start"), page.getByRole("link", { name: "Skip to main content" })]) {
    await page.keyboard.press("Shift"); // keyboard modality, so :focus-visible applies to the scripted focus
    await el.focus();
    const o = await el.evaluate((n) => { const s = getComputedStyle(n); return { style: s.outlineStyle, width: parseFloat(s.outlineWidth), shadow: s.boxShadow }; });
    expect(o.style !== "none" && o.width >= 2 || o.shadow !== "none", `focus indicator on ${await el.evaluate((n) => n.outerHTML.slice(0, 60))}`).toBe(true);
  }
});

test("colour tokens meet WCAG AA contrast on every surface they are used on", async ({ app }) => {
  const t = await app.page.evaluate(() => {
    const cs = getComputedStyle(document.documentElement);
    return Object.fromEntries(["--bg", "--panel", "--panel2", "--text", "--muted", "--accent", "--accent-ink", "--ok", "--warn", "--bad", "--info"].map((k) => [k, cs.getPropertyValue(k).trim()]));
  });
  const lum = (hex: string) => { const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  const ratio = (a: string, b: string) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
  const pairs: [string, string][] = [["--text", "--bg"], ["--text", "--panel"], ["--text", "--panel2"], ["--muted", "--bg"], ["--muted", "--panel"], ["--muted", "--panel2"],
    ["--accent-ink", "--accent"], ["--ok", "--panel"], ["--warn", "--panel"], ["--bad", "--panel"], ["--info", "--panel"]];
  for (const [fg, bg] of pairs) expect(ratio(t[fg], t[bg]), `${fg} on ${bg}`).toBeGreaterThanOrEqual(4.5);
});

test("errors are announced through an alert region", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("AI agents");
  await page.getByRole("button", { name: "5. Masking Recommendation" }).click();
  await app.click("Run agent"); // no project selected: the API refuses and the UI must say so
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("alert")).toContainText(/project_id/);
});

test("status is never conveyed by colour alone", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Landscape");
  for (const b of await page.locator(".badge").all()) expect((await b.innerText()).trim().length, "a badge without text").toBeGreaterThan(0);
});

test("small screens: no horizontal page scroll in any view, navigation opens from the menu button, 320px reflow", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 800 });
    for (const v of ["Control tower", "Landscape", "Selective designer", "Delta refresh", "Lean client", "Full refresh", "Post-copy", "Orchestration", "Test catalog", "AI agents"]) {
      await page.getByRole("button", { name: "Toggle navigation" }).click();
      await page.getByRole("navigation", { name: "Main" }).getByRole("button", { name: v, exact: true }).click();
      await app.settle();
      const over = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
      expect(over.sw, `${v} at ${width}px overflows horizontally`).toBeLessThanOrEqual(over.cw);
    }
  }
  await expectAccessible(page, "AI agents at 320px");
});

test("the colour-scheme preference does not break accessibility (only a dark theme exists today)", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  for (const scheme of ["dark", "light"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await app.nav("Control tower");
    await expectAccessible(page, `control tower (${scheme})`);
  }
});
