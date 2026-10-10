import { useState } from "react";
import { useApi, useProjectDetails } from "../hooks";
import { Card, ErrorBox, Pill, Table } from "../components/ui";

export default function Catalog() {
  const { source } = useProjectDetails();
  const cat = useApi<any[]>("/catalog/business-objects");
  const inv = useApi<any>(source ? `/systems/${source.id}/business-objects` : null, { limit: 1 }, [source?.id]);
  const [sel, setSel] = useState<any>(null);
  const [bukrs, setBukrs] = useState("");
  const inst = useApi<any>(source && sel ? `/systems/${source.id}/business-objects` : null, { object_type: sel?.id, bukrs, limit: 100 }, [sel?.id, bukrs]);
  return (
    <div className="grid2">
      <Card title="Canonical business object model"><ErrorBox error={cat.error} /><Table cols={[{ k: "id", h: "Id", r: (r) => <a href="#" onClick={(e) => { e.preventDefault(); setSel(r); }}>{r.id}</a> }, { k: "name", h: "Name" }, { k: "domain", h: "Domain" }, { k: "kind", h: "Kind" }, { k: "header_table", h: "Leading table" }, { k: "org_scope", h: "Org scope" }, { k: "n", h: "Instances", r: (r) => inv.data?.counts?.[r.id] ?? "-" }]} rows={cat.data || []} /></Card>
      <Card title={sel ? `${sel.name} (${sel.id})` : "Select an object"} actions={sel && <input placeholder="company code" value={bukrs} onChange={(e) => setBukrs(e.target.value)} style={{ width: 120 }} />}>
        {sel && <>
          <p><b>Tables:</b> {sel.header_table} {sel.item_tables.join(", ")} · <b>Keys:</b> {sel.key_fields.join(", ")} · <b>Applicability:</b> {sel.applicability.join(", ")}</p>
          {sel.s4_simplification && <p><b>S/4HANA:</b> {sel.s4_simplification}</p>}
          <Table cols={[{ k: "target", h: "Target" }, { k: "method", h: "Load method", r: (r) => <Pill value={r.method} /> }, { k: "api", h: "API / object" }, { k: "note", h: "Note" }]} rows={sel.load_methods} />
          <h4>Instances</h4>
          <Table cols={[{ k: "key", h: "Key" }, { k: "bukrs", h: "Company code" }, { k: "werks", h: "Plant" }, { k: "gjahr", h: "Year" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "company_codes", h: "Touches", r: (r) => r.company_codes.join(", ") }]} rows={inst.data?.items} />
        </>}
      </Card>
    </div>
  );
}
