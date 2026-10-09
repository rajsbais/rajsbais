import AxeBuilder from "@axe-core/playwright";
import { test as base, expect, Page } from "@playwright/test";
import { ChildProcess, spawn } from "node:child_process";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";
import path from "node:path";

const BACKEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../backend");

const freePort = () => new Promise<number>((resolve, reject) => {
  const s = createServer();
  s.listen(0, "127.0.0.1", () => { const p = (s.address() as { port: number }).port; s.close(() => resolve(p)); });
  s.on("error", reject);
});

async function startBackend(): Promise<{ url: string; stop: () => Promise<void> }> {
  const port = await freePort();
  const proc: ChildProcess = spawn("python", ["-m", "uvicorn", "rfactory.api.main:app", "--port", String(port), "--log-level", "warning"],
    { cwd: BACKEND, stdio: "ignore", detached: true });
  const url = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${url}/api/health`)).ok) break; } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 100));
    if (i === 99) throw new Error("backend did not start");
  }
  return { url, stop: async () => { try { process.kill(-proc.pid!, "SIGTERM"); } catch { /* already gone */ } } };
}

/** The Keystone UI as a user drives it. Every method uses roles and labels, never CSS selectors. */
export class Keystone {
  readonly errors: string[] = [];
  constructor(readonly page: Page, readonly url: string) {
    page.on("dialog", (d) => void d.accept());
    page.on("pageerror", (e) => this.errors.push(`pageerror: ${e.message}`));
    page.on("console", (m) => { if (m.type() === "error") this.errors.push(`console: ${m.text()}`); });
  }
  async open() { await this.page.goto(this.url); await expect(this.page.getByRole("heading", { level: 1 }).or(this.page.getByText("Simulated SAP")).first()).toBeVisible(); }
  async loadLandscape() { await this.page.getByRole("button", { name: "Load synthetic landscape" }).click(); await expect(this.page.getByRole("navigation").getByRole("button", { name: "Landscape" })).toBeVisible(); await this.settle(); }
  async as(user: string) { await this.page.getByLabel("Signed in as").or(this.page.locator("#user")).first().selectOption(user); await this.settle(); }
  async nav(label: string) { await this.page.getByRole("navigation").getByRole("button", { name: label, exact: true }).click(); await this.settle(); }
  async click(name: string | RegExp, exact = true) { await this.page.getByRole("button", { name, exact: typeof name === "string" ? exact : undefined }).first().click(); await this.settle(); }
  async settle() { await this.page.waitForLoadState("networkidle"); await this.page.waitForTimeout(150); }
  api(user: string) {
    const url = this.url;
    const call = async (method: string, p: string, body?: unknown) => {
      const r = await fetch(`${url}${p}`, { method, headers: { "X-Demo-User": user, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
      return { status: r.status, body: await r.json().catch(() => null) };
    };
    return { get: (p: string) => call("GET", p), post: (p: string, b?: unknown) => call("POST", p, b ?? {}), put: (p: string, b?: unknown) => call("PUT", p, b ?? {}) };
  }
}

/** Fails with a readable list if the current screen has WCAG A/AA (or axe best-practice) violations. */
export async function expectAccessible(page: Page, label: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]).analyze();
  expect(r.violations.map((v) => `${v.id} (${v.impact}): ${v.help} — e.g. ${v.nodes[0].target.join(" ")}`), `accessibility of ${label}`).toEqual([]);
}

export const test = base.extend<{ app: Keystone }>({
  app: async ({ page }, use) => {
    const be = await startBackend();
    const app = new Keystone(page, be.url);
    await app.open();
    await use(app);
    await be.stop();
  },
});
export { expect };
