import AxeBuilder from "@axe-core/playwright";
import { Page } from "@playwright/test";
import { expect, test } from "./fixtures";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

/** Scans one screen and returns its violations; the tests collect every screen first so one run shows all problems. */
async function scan(page: Page, label: string): Promise<string[]> {
  const r = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  return r.violations.map((v) => `[${label}] ${v.id} (${v.impact}): ${v.help} — ${v.nodes.length} node(s), e.g. ${v.nodes[0].target.join(" ")}`);
}

const VIEWS = ["Control tower", "Landscape", "Readiness", "Selective designer", "Delta refresh", "Dependencies", "Conflicts", "Masking", "Execution",
  "Reconciliation", "Lean client", "Full refresh", "Post-copy", "Orchestration", "Benchmarks", "Test catalog", "Audit", "AI agents"];

test("every view of the empty app has no accessibility violations", async ({ app }) => {
  const found: string[] = [];
  for (const v of VIEWS) {
    const btn = app.page.getByRole("navigation").getByRole("button", { name: v, exact: true });
    if (await btn.isDisabled()) continue;
    await app.nav(v);
    found.push(...await scan(app.page, `${v} (empty)`));
  }
  expect(found).toEqual([]);
});

test("every view with data loaded has no accessibility violations", async ({ app }) => {
  await app.loadLandscape();
  await app.as("root.admin"); // sees every view, including Audit
  const found: string[] = [];
  for (const v of VIEWS) {
    await app.nav(v);
    found.push(...await scan(app.page, v));
  }
  expect(found).toEqual([]);
});
