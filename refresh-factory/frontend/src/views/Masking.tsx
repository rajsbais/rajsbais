import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable } from "../components";
import { useApp } from "../ctx";

export default function Masking() {
  const { project } = useApp();
  const [tm, setTm] = useState<J[]>([]);
  const [st, setSt] = useState<J>(null);
  useEffect(() => { api.get("/api/masking/templates").then(setTm).catch(() => undefined); }, []);
  useEffect(() => { setSt(null); if (project?.manifest) api.get(`/api/projects/${project.id}/masking`).then(setSt).catch(() => undefined); }, [project?.id, project?.status, project?.manifest_hash]); // eslint-disable-line react-hooks/exhaustive-deps
  const cls = (r: J) => <Badge kind={r.protection_class.includes("reversible") ? "bad" : r.protection_class.startsWith("anonym") ? "ok" : "warn"}>{r.protection_class}</Badge>;
  return (
    <>
      <Card title="Policy templates">
        <div className="grid two">{tm.map((t) => (
          <div key={t.id} className="tpl"><h3>{t.name}</h3><p className="muted">{t.description}</p><p>{t.rules.length} rules · {cls(t.rules[0])}</p></div>))}</div>
        <p className="muted small">Pseudonymization is reversible by anyone holding the key plus candidate values and is never labelled “anonymization”. Per-run anonymization destroys its key after the run, and still carries residual inference risk from unmasked quasi-identifiers (city, dates, amounts).</p>
      </Card>
      {st?.policy && (
        <Card title={`Active policy — ${st.policy.name}`}>
          <DataTable rows={st.policy.rules} cols={[
            { key: "table", title: "Table" }, { key: "field", title: "Field" }, { key: "category", title: "Category" }, { key: "strategy", title: "Strategy" },
            { key: "mode", title: "Mode" }, { key: "protection_class", title: "Protection class", render: cls }]} />
        </Card>)}
      {st && (
        <Card title="Sensitive-field discovery on the planned scope">
          <DataTable rows={st.discovered} cols={[
            { key: "table", title: "Table" }, { key: "field", title: "Field" }, { key: "category", title: "Category" },
            { key: "source", title: "Found by" }, { key: "confidence", title: "Confidence" },
            { key: "covered", title: "Covered by a rule", render: (d) => <Badge kind={d.covered ? "ok" : "bad"}>{d.covered ? "yes" : "NO — blocks release"}</Badge> }]}
            empty="Build a plan to run discovery" />
        </Card>)}
      {!project && <p className="muted">Select a project to see its masking configuration.</p>}
    </>
  );
}
