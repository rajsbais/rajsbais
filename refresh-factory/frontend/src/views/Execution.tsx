import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Card, ErrorNote, StatusBadge, Stat, useAction } from "../components";
import { useApp } from "../ctx";

export default function Execution() {
  const app = useApp();
  const [run, setRun] = useState<J>(null);
  const act = useAction();
  const load = () => { if (app.runId) api.get(`/api/runs/${app.runId}`).then(setRun).catch(() => setRun(null)); };
  useEffect(load, [app.runId]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!app.runId || !run) return <Card><p className="muted">No run selected. Execute an approved project from the Designer.</p></Card>;
  const pct = run.total_objects ? Math.round((run.checkpoint / run.total_objects) * 100) : 0;
  const mutate = (path: string) => act.run(async () => { setRun(await api.post(path)); await app.reload(); });
  return (
    <>
      <div className="grid stats">
        <Stat label="Run" value={<code>{run.id}</code>} /><Stat label="Status" value={<StatusBadge s={run.status} />} />
        <Stat label="Objects loaded" value={`${run.loaded} / ${run.total_objects}`} /><Stat label="Quarantined / skipped" value={`${run.quarantined} / ${run.skipped}`} />
      </div>
      <Card title="Progress and checkpoint" actions={<>
        <button disabled={run.status !== "FAILED" || act.busy || !app.can("run:execute")} onClick={() => mutate(`/api/runs/${run.id}/resume`)}>Resume from checkpoint</button>
        <button className="danger" disabled={!["FAILED", "COMPLETED"].includes(run.status) || act.busy || !app.can("run:execute")} onClick={() => confirm("Roll back every change this run made to the target?") && mutate(`/api/runs/${run.id}/rollback`)}>Roll back</button></>}>
        <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}><div style={{ width: `${pct}%` }} /></div>
        <p className="muted">{pct}% · attempts {run.attempts} · {run.error ?? "no errors"}</p>
        <ErrorNote error={act.error} />
      </Card>
      <Card title="Job timeline">
        <ol className="timeline">{run.steps.map((s: J) => (
          <li key={s.name}><StatusBadge s={s.status} /> <strong>{s.name}</strong> <span className="muted small">{s.started ?? ""} → {s.finished ?? ""}</span>
            <pre>{JSON.stringify(s.detail)}</pre></li>))}</ol>
      </Card>
      <Card title="Event log"><ul className="log">{run.events.map((e: J, i: number) => <li key={i}><span className="muted small">{e.ts}</span> <StatusBadge s={e.level} /> {e.msg}</li>)}</ul></Card>
    </>
  );
}
