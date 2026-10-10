import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, expectAccessible, Keystone, startBackend, test } from "./fixtures";

// The second test stops and starts the backend itself, on a data directory, to prove state survives a restart.
test("the platform says whether it is durable", async ({ app }) => {
  await app.nav("Readiness");
  await expect(page(app).getByText("in memory", { exact: true })).toBeVisible(); // the default test backend has no RFACTORY_DATA_DIR
  await expectAccessible(page(app), "readiness (in-memory)");
});

const page = (app: Keystone) => app.page;

test("work survives a backend restart: the project and the landscape are still there", async ({ page: pg }) => {
  const dir = mkdtempSync(path.join(tmpdir(), "keystone-e2e-"));
  const env = { RFACTORY_DATA_DIR: dir, RFACTORY_STATE_KEY: "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=" };
  let be = await startBackend(env);
  try {
    const call = async (url: string, user: string, method: string, p: string, body?: unknown) => {
      const r = await fetch(`${url}${p}`, { method, headers: { "X-Demo-User": user, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
      return r.json();
    };
    const b = await call(be.url, "root.admin", "POST", "/api/demo/bootstrap");
    const prj = await call(be.url, "alice.basis", "POST", "/api/projects", { name: "survives restart", source_id: b.source.id, target_id: b.target.id });
    expect(prj.id).toBeTruthy();
    await be.stop();

    be = await startBackend(env); // a new process on the same data directory
    const app = new Keystone(pg, be.url);
    await app.open();
    await app.nav("Readiness");
    await expect(pg.getByText("durable", { exact: true })).toBeVisible();
    await expect(pg.getByText(/encrypted at rest/)).toBeVisible();
    await expectAccessible(pg, "readiness (durable)");
    const projects = await call(be.url, "alice.basis", "GET", "/api/projects");
    expect(projects.map((p: { name: string }) => p.name)).toEqual(["survives restart"]);
    await app.nav("Landscape");
    await expect(pg.getByText("EP1/100").first()).toBeVisible(); // the landscape came back without "Load synthetic landscape"
    expect(app.errors).toEqual([]);
  } finally {
    await be.stop();
  }
});
