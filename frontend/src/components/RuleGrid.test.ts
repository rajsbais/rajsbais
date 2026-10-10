import { describe, expect, it } from "vitest";
import { describeRule, linesToMap, mapToLines, whenText } from "./RuleGrid";

describe("rule grid helpers", () => {
  it("round-trips mappings as key = value lines", () => {
    expect(linesToMap("5000 = SP01\n5100 -> SP02\n140000: 12100000\n\nbad line\n")).toEqual({ "5000": "SP01", "5100": "SP02", "140000": "12100000" });
    expect(mapToLines({ "5000": "SP01", "5100": "SP02" })).toBe("5000 = SP01\n5100 = SP02");
    expect(linesToMap(mapToLines({ a: "1", b: "2" }))).toEqual({ a: "1", b: "2" });
    expect(mapToLines(undefined)).toBe("");
  });
  it("describes rules the way the grid endpoint does", () => {
    expect(describeRule({ type: "org_reassign", field: "BUKRS", also_fields: ["VBUND"], map: { "5000": "SP01" } })).toEqual({ fields: "BUKRS, VBUND", mapping: "5000 → SP01", condition: "" });
    expect(describeRule({ type: "value_map", fields: ["HKONT", "SAKNR"], lookup: "coa" }, { coa: { a: "1", b: "2" } }).mapping).toBe("lookup coa (2 entries)");
    expect(describeRule({ type: "value_map", fields: ["X"], map: { a: 1, b: 2, c: 3, d: 4, e: 5 } }).mapping).toBe("a → 1, b → 2, c → 3 (+2)");
    expect(describeRule({ type: "key_map", fields: ["KUNNR"], strategy: "prefix", prefix: "BP", when: { field: "KUNNR", not_prefix: "BP" } })).toEqual({ fields: "KUNNR", mapping: "prefix BP", condition: "KUNNR not prefix BP" });
    expect(describeRule({ type: "number_range", fields: ["BELNR"], offset: 500000000 }).mapping).toBe("offset 500000000");
    expect(describeRule({ type: "currency_convert", fields: ["DMBTR"], to: "EUR", rates: { "USD->EUR": 0.9 } }).mapping).toBe("to EUR (1 rates)");
    expect(describeRule({ type: "default", set: { RLDNR: "0L" } })).toEqual({ fields: "RLDNR", mapping: "RLDNR = 0L", condition: "" });
    expect(describeRule({ type: "conditional", when: { field: "BLART", equals: "RV" }, then: { set: { XREF1: "MIG" } } })).toEqual({ fields: "XREF1", mapping: "XREF1 = MIG", condition: "BLART equals RV" });
    expect(describeRule({ type: "field_map", map: { NAME1: "BU_NAME1" } })).toEqual({ fields: "NAME1", mapping: "NAME1 → BU_NAME1", condition: "" });
    expect(describeRule({ type: "lookup_enrich", key_field: "KOSTL", lookup: "cc_to_pc", set_field: "PRCTR" }).mapping).toBe("PRCTR from cc_to_pc by KOSTL");
    expect(describeRule({ type: "reject", when: { field: "BSTAT", in: ["S", "V"] }, message: "parked" })).toEqual({ fields: "", mapping: "parked", condition: 'BSTAT in ["S","V"]' });
  });
  it("renders composed conditions", () => {
    expect(whenText({ all: [{ field: "A", present: true }, { any: [{ field: "B", in_lookup: "l" }, { field: "C", prefix: "Z" }] }] })).toBe("A present true and B in lookup l or C prefix Z");
    expect(whenText(undefined)).toBe("");
  });
});
