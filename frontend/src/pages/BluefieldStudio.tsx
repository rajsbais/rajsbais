import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, Pill, Table } from "../components/ui";

export default function BluefieldStudio() {
  const { source, target } = useProjectDetails();
  const compat = useApi<any[]>("/catalog/s4-compatibility");
  const cat = useApi<any[]>("/catalog/business-objects");
  const adapters = useApi<any>("/catalog/adapters");
  return (
    <div>
      <Banner kind="warn">Bluefield / hybrid conversion: the target is a prepared S/4HANA shell (configuration-based). Load strategies below are selected from the release-specific compatibility registry; the loaders themselves are <b>simulated</b> in this build. Finance conversion, CVI execution and simplification-item remediation are planned.</Banner>
      <Card title={`Source ${source?.product || "?"} ${source?.release || ""} → Target ${target?.product || "?"} ${target?.release || ""}`}>
        <Table cols={[{ k: "item", h: "Simplification / conversion item" }, { k: "affects", h: "Affects", r: (r) => r.affects.join(", ") }, { k: "ecc", h: "ECC" }, { k: "s4", h: "S/4HANA" }, { k: "action", h: "Engine action" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }]} rows={compat.data || []} />
      </Card>
      <div className="grid2">
        <Card title="Load strategy per business object (target S/4HANA)"><Table cols={[{ k: "id", h: "Object" }, { k: "m", h: "Method", r: (r) => <Pill value={r.load_methods.find((x: any) => x.target === "S4HANA")?.method || "NONE"} /> }, { k: "api", h: "API / object", r: (r) => r.load_methods.find((x: any) => x.target === "S4HANA")?.api }, { k: "s4_simplification", h: "Note" }]} rows={cat.data || []} /></Card>
        <Card title="Connectivity adapters"><Table cols={[{ k: "name", h: "Adapter" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "description", h: "Description" }]} rows={Object.entries(adapters.data || {}).map(([name, v]: any) => ({ name, ...v }))} /><p className="muted">Direct writes to S/4HANA application tables are never performed. Objects whose only method is DIRECT_TABLE_UNSUPPORTED are reported as unsupported at load time.</p></Card>
      </div>
    </div>
  );
}
