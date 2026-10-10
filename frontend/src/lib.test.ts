import { describe, expect, it } from "vitest";
import { auditVerdict, crossCompany, dbSize, groupFor, NAV, pageFor, reconciliationStatus, sharedRisk, terabytes } from "./lib";

describe("transform factory helpers", () => {
  it("bands the shared-data exposure of a classification", () => {
    expect(sharedRisk({ FULLY_TRANSFERRED: 90, SHARED_DUPLICATED: 5 })).toMatchObject({ band: "LOW", shared: 5, total: 95 });
    expect(sharedRisk({ FULLY_TRANSFERRED: 70, REFERENCE_ONLY: 20, MANUAL_DISPOSITION: 5 }).band).toBe("MEDIUM");
    expect(sharedRisk({ FULLY_TRANSFERRED: 10, SHARED_DUPLICATED: 20 }).band).toBe("HIGH");
    expect(sharedRisk(undefined)).toMatchObject({ band: "LOW", share: 0, total: 0 });
    expect(crossCompany({ PARTIALLY_TRANSFERRED: 3 })).toBe(3);
    expect(crossCompany({})).toBe(0);
  });
  it("formats terabytes and verdicts", () => {
    expect(terabytes(17.2 * 1024 ** 4)).toBe("17.2");
    expect(terabytes(0)).toBe("0.0");
    expect(dbSize(17.2 * 1024 ** 4)).toEqual({ value: "17.2", unit: "TB" });
    expect(dbSize(42 * 1024 ** 3)).toEqual({ value: "42.0", unit: "GB" });
    expect(dbSize(3 * 1024 ** 2)).toEqual({ value: "3.0", unit: "MB" });
    expect(dbSize(undefined)).toEqual({ value: "0.0", unit: "MB" });
    expect(auditVerdict(null)).toBe("NO_RUN");
    expect(auditVerdict({ status: "RUNNING" })).toBe("RUNNING");
    expect(auditVerdict({ status: "COMPLETED", reconciliation: "FAIL" })).toBe("VARIANCES_OPEN");
    expect(auditVerdict({ status: "COMPLETED", reconciliation: "PASS" })).toBe("VERTICAL_SLICE_COMPLETE");
    expect(reconciliationStatus("PASS")).toEqual({ label: "RECONCILED", cls: "ok" });
    expect(reconciliationStatus("WARN").cls).toBe("warn");
    expect(reconciliationStatus(undefined).label).toBe("NOT RECONCILED");
  });
  it("maps every route to one primary group with a title", () => {
    const routes = NAV.flatMap((g) => g.items.map((i) => i.to));
    expect(new Set(routes).size).toBe(routes.length);
    expect(NAV.map((g) => g.label)).toEqual(["Dashboard", "Landscape", "Carve-out", "Dependencies", "Connect", "Extract", "Rules", "CDC", "Finance", "Cutover", "Audit"]);
    expect(groupFor("/reconciliation").id).toBe("finance");
    expect(groupFor("/runs").id).toBe("extract");
    expect(groupFor("/unknown").id).toBe("dashboard");
    expect(pageFor("/audit")?.title).toBe("Audit report");
  });
});
