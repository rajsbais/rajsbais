import { useCallback, useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable, ErrorNote, Stat, useAction } from "../components";
import { useApp } from "../ctx";

const PHASES = ["extract", "mask_stage", "load", "reconcile"];

export default function Benchmarks() {
  const app = useApp();
  const [sum, setSum] = useState<J>(null); const [samples, setSamples] = useState<J[]>([]);
  const [src, setSrc] = useState(""); const [tgt, setTgt] = useState(""); const [rows, setRows] = useState(5000);
  const [est, setEst] = useState<J>(null); const [ran, setRan] = useState<J>(null);
  const [imp, setImp] = useState({ phase: "load", rows: 10000, seconds: 30, environment: "real:QAS-copy-1", label: "" });
  const act = useAction();

  const load = useCallback(async () => {
    setSum(await api.get("/api/benchmark/summary")); setSamples(await api.get("/api/benchmark/samples"));
  }, []);
  useEffect(() => { if (!src && app.systems[0]) { setSrc(app.systems[0].id); setTgt((app.systems.find((s) => s.role !== "PRD") ?? app.systems[0]).id); } }, [app.systems.length]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { act.run(load); }, [load, app.me?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const go = (fn: () => Promise<unknown>) => act.run(async () => { await fn(); await load(); });

  return (
    <>
      <Card title="Benchmarks and estimates" actions={<button className="primary" disabled={!app.can("run:execute") || !src || act.busy}
        onClick={() => go(async () => setRan(await api.post("/api/benchmark/run", { source_id: src })))}>Run benchmark on source</button>}>
        <p className="muted">Duration estimates are fitted from <strong>measured</strong> samples, per phase and per environment: seconds = a + b·rows, with an 80% prediction interval and a leave-one-out error.
          A phase with too little data is refused rather than guessed, and until every phase is calibrated the estimate falls back to a labelled placeholder.
          Timings of the simulator and of the masking engine measure <strong>this program on this host, not SAP</strong>; imported measurements are declared by the importer and cannot be verified here.</p>
        {sum && <div className="grid stats">
          <Stat label="Samples" value={sum.samples} hint={`${sum.excluded} excluded`} />
          <Stat label="Environments" value={sum.environments.length} hint={sum.environments.join(", ") || "none yet"} />
          <Stat label="Recent error" value={sum.accuracy.recent_mape == null ? "n/a" : `${Math.round(sum.accuracy.recent_mape * 100)}%`} hint={`${sum.accuracy.runs} scored run(s)`} />
          <Stat label="Interval coverage" value={sum.accuracy.interval_coverage == null ? "n/a" : `${Math.round(sum.accuracy.interval_coverage * 100)}%`} hint="nominal 80%" />
        </div>}
        {ran && <p className="muted small">Benchmark added {ran.samples_added} sample(s) in {ran.environment}.</p>}
        <ErrorNote error={act.error} />
      </Card>

      <Card title="Fitted models">
        <DataTable rows={sum?.models ?? []} empty="No samples yet. Run the benchmark, or build plans and run refreshes." cols={[
          { key: "phase", title: "Phase" }, { key: "environment", title: "Environment" }, { key: "samples", title: "Samples" },
          { key: "rps", title: "Rows/s", render: (m: J) => m.model ? m.model.rows_per_second.toLocaleString() : "—" },
          { key: "int", title: "Fixed cost (s)", render: (m: J) => m.model ? m.model.intercept_s : "—" },
          { key: "q", title: "Quality", render: (m: J) => m.model ? <Badge kind={m.model.quality === "good" ? "pass" : m.model.quality === "fair" ? "warn" : "fail"}>{m.model.quality}</Badge> : <span className="muted small">{m.reason}</span> },
          { key: "d", title: "Source", render: (m: J) => m.declared ? <Badge kind="warn">includes declared</Badge> : <Badge kind="info">measured here</Badge> }]} />
      </Card>

      <Card title="Estimate calculator">
        <div className="row">
          <label>Source <select value={src} onChange={(e) => setSrc(e.target.value)}>{app.systems.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
          <label>Target <select value={tgt} onChange={(e) => setTgt(e.target.value)}>{app.systems.filter((s) => s.role !== "PRD").map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
          <label>Rows <input type="number" min={1} value={rows} onChange={(e) => setRows(Number(e.target.value))} /></label>
          <button disabled={!src || act.busy} onClick={() => act.run(async () => setEst(await api.post("/api/benchmark/estimate", { rows, bytes: rows * 100, source_id: src, target_id: tgt })))}>Estimate</button>
        </div>
        {est && <div aria-live="polite">
          <p className="mono big">{est.seconds}s {est.basis === "calibrated" ? <Badge kind="pass">calibrated</Badge> : <Badge kind="warn">placeholder</Badge>}
            {est.low != null && <span className="muted small"> 80% interval {est.low}–{est.high}s{est.extrapolated ? " (extrapolated: wider)" : ""}</span>}</p>
          <p className="muted small">{est.disclaimer}</p>
          {est.environment_note && <p className="muted small">{est.environment_note}</p>}
          {Object.keys(est.phases).length > 0 && <DataTable rows={Object.entries(est.phases).map(([k, v]: J) => ({ phase: k, ...v }))} cols={[
            { key: "phase", title: "Phase" }, { key: "environment", title: "Environment" }, { key: "seconds", title: "Seconds", render: (p: J) => p.seconds },
            { key: "range", title: "Interval", render: (p: J) => `${p.low}–${p.high}` }, { key: "quality", title: "Quality" }]} />}
        </div>}
      </Card>

      <Card title="Import a measurement">
        <p className="muted small">For timings taken in a controlled test on a real system. The environment label keeps them apart from simulator data; 'simulated' and 'platform' are reserved.</p>
        <div className="row">
          <label>Phase <select value={imp.phase} onChange={(e) => setImp({ ...imp, phase: e.target.value })}>{PHASES.map((p) => <option key={p}>{p}</option>)}</select></label>
          <label>Rows <input type="number" min={1} value={imp.rows} onChange={(e) => setImp({ ...imp, rows: Number(e.target.value) })} /></label>
          <label>Seconds <input type="number" min={0} step="any" value={imp.seconds} onChange={(e) => setImp({ ...imp, seconds: Number(e.target.value) })} /></label>
          <label>Environment <input value={imp.environment} onChange={(e) => setImp({ ...imp, environment: e.target.value })} /></label>
          <button disabled={!app.can("system:write") || act.busy} onClick={() => go(() => api.post("/api/benchmark/samples", imp))}>Import sample</button>
        </div>
      </Card>

      <Card title="Samples">
        <DataTable rows={samples} empty="No samples." cols={[
          { key: "phase", title: "Phase" }, { key: "environment", title: "Environment" }, { key: "rows", title: "Rows" },
          { key: "seconds", title: "Seconds", render: (s: J) => s.seconds.toFixed(4) }, { key: "origin", title: "Origin", render: (s: J) => <span>{s.origin}{s.declared ? " (declared)" : ""}</span> },
          { key: "label", title: "Label" },
          { key: "x", title: "", render: (s: J) => <button disabled={!app.can("system:write") || act.busy} onClick={() => go(() => api.post(`/api/benchmark/samples/${s.id}/exclude?excluded=${!s.excluded}`))}>{s.excluded ? "Restore" : "Exclude"}</button> }]} />
      </Card>

      <Card title="Prediction accuracy">
        <DataTable rows={sum?.accuracy.history ?? []} empty="Scored once a run completes after a calibrated estimate was shown." cols={[
          { key: "project", title: "Project" }, { key: "predicted", title: "Predicted (s)" }, { key: "actual", title: "Actual (s)" },
          { key: "error", title: "Error", render: (a: J) => a.error == null ? "—" : `${Math.round(a.error * 100)}%` },
          { key: "w", title: "In interval", render: (a: J) => a.within_interval ? <Badge kind="pass">yes</Badge> : <Badge kind="fail">no</Badge> }]} />
      </Card>
    </>
  );
}
