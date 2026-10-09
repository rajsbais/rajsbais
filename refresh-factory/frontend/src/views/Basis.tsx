import { useCallback, useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, ErrorNote, StatusBadge, useAction } from "../components";
import { useApp } from "../ctx";

export function Readiness() {
  const { systems } = useApp();
  const [rows, setRows] = useState<J[]>([]);
  useEffect(() => { Promise.all(systems.map((s) => api.get(`/api/systems/${s.id}/readiness`))).then(setRows).catch(() => undefined); }, [systems]);
  return (
    <Card title="Source and target readiness">
      {rows.map((r) => (
        <div key={r.system} className="ready-row"><strong>{r.system}</strong> <StatusBadge s={r.ready ? "pass" : "fail"} />
          <ul className="checks">{r.checks.map((c: J) => <li key={c.id}>{c.ok ? "✓" : "✗"} {c.name}</li>)}</ul></div>))}
      {!rows.length && <p className="muted">Register systems from the Control tower first.</p>}
    </Card>
  );
}

export function FullRefresh() {
  const app = useApp();
  const [progs, setProgs] = useState<J[]>([]); const [profiles, setProfiles] = useState<J[]>([]); const [sel, setSel] = useState<string | null>(null);
  const [src, setSrc] = useState(""); const [tgt, setTgt] = useState(""); const [prof, setProf] = useState(""); const [backup, setBackup] = useState("HANA-BKP-2026-10-09");
  const act = useAction();
  const prds = app.systems.filter((s) => s.role === "PRD"); const targets = app.systems.filter((s) => s.role !== "PRD");
  const load = useCallback(async () => { setProgs(await api.get("/api/full-refresh/programs")); setProfiles(await api.get("/api/postcopy/profiles")); }, []);
  useEffect(() => { act.run(load); }, [load, app.me?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (!src && prds[0]) setSrc(prds[0].id); if (!tgt && targets[0]) setTgt(targets[0].id); }, [app.systems.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const okProfiles = profiles.filter((p) => p.system_id === tgt && p.status === "APPROVED");
  const p = progs.find((x) => x.id === sel) ?? progs[0];
  const go = (path: string, body?: unknown) => act.run(async () => { await api.post(`/api/full-refresh/programs/${p.id}/${path}`, body ?? {}); await load(); });
  const waitingPc = p?.status === "WAITING" && p.checkpoint === 7;
  return (
    <>
      <Card title="Full system refresh programs">
        <p><Badge kind="warn">simulated copy mechanism</Badge> <span className="muted">The 13-phase program is real orchestration logic against simulated systems. The copy itself is an in-memory stand-in for SWPM / HANA backup-recovery / snapshots, which are not built. The target holds <strong>unmasked</strong> production data from phase 7 until masking is verified in phase 11.</span></p>
        <div className="form">
          <label>Source<select aria-label="Source system" value={src} onChange={(e) => setSrc(e.target.value)}>{prds.map((s) => <option key={s.id} value={s.id}>{s.label} · {s.family}</option>)}</select></label>
          <label>Target<select aria-label="Target system" value={tgt} onChange={(e) => setTgt(e.target.value)}>{targets.map((s) => <option key={s.id} value={s.id}>{s.label} · {s.family}</option>)}</select></label>
          <label>Approved pre-copy profile<select aria-label="Pre-copy profile" value={prof} onChange={(e) => setProf(e.target.value)}><option value="">—</option>{okProfiles.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></label>
          <label>Backup / recovery plan reference<input aria-label="Backup reference" value={backup} onChange={(e) => setBackup(e.target.value)} /></label>
        </div>
        <button className="primary" disabled={!app.can("plan:write") || !prof || act.busy} onClick={() => act.run(async () => { const r = await api.post("/api/full-refresh/programs", { source_id: src, target_id: tgt, profile_id: prof, backup_ref: backup }); setSel(r.id); await load(); })}>Create program</button>
        {!okProfiles.length && <span className="muted"> Capture, submit and approve a profile of the target on the Post-copy page <em>before</em> the copy.</span>}
        <ErrorNote error={act.error} />
      </Card>
      {p && (
        <Card title={`${p.name}`} actions={<select aria-label="Program" value={p.id} onChange={(e) => setSel(e.target.value)}>{progs.map((x) => <option key={x.id} value={x.id}>{x.name} · {x.status}</option>)}</select>}>
          <div className="row"><StatusBadge s={p.status} />{p.unmasked_target && <Badge kind="bad">target holds UNMASKED data</Badge>}<span className="muted small">hash {p.hash.slice(0, 12)} · attempt {p.attempts} · {p.mechanism_note}</span></div>
          {p.reasons.map((r: string) => <p key={r}><StatusBadge s="fail" /> {r}</p>)}
          {p.waiting_for.length > 0 && <p><Badge kind="warn">waiting</Badge> {p.waiting_for.join(" · ")}{waitingPc && <> — approve on the <button onClick={() => app.go("postcopy")}>Post-copy</button> page, then continue</>}</p>}
          <ol className="phases">{p.phases.map((ph: J) => (
            <li key={ph.no}><strong>{ph.name}</strong> <StatusBadge s={ph.status === "DONE" ? "pass" : ph.status === "HELD" || ph.status === "FAILED" ? "fail" : ph.status === "WAITING" ? "warn" : "info"} /> <span className="muted small">{ph.status} · {ph.performed_by} · rollback: {ph.rollback}</span>
              {ph.detail && Object.keys(ph.detail).length > 0 && <div className="muted small mono">{JSON.stringify(ph.detail).slice(0, 220)}</div>}</li>))}</ol>
          <div className="row">
            <button disabled={!app.can("plan:approve") || !!p.approvals.change_approver || act.busy} onClick={() => go("approve", { label: "change_approver" })}>Approve as change approver</button>
            <button disabled={!app.can("postcopy:approve:basis_lead") || !!p.approvals.basis_lead || act.busy} onClick={() => go("approve", { label: "basis_lead" })}>Confirm backup (Basis lead)</button>
            <button className="primary" disabled={!app.can("run:execute") || !["APPROVED", "WAITING", "HELD", "FAILED"].includes(p.status) || act.busy} onClick={() => go("run")}>{p.checkpoint > 2 ? "Continue" : "Run"}</button>
            <button disabled={!app.can("postcopy:approve:security_officer") || !(p.status === "WAITING" && p.checkpoint === 11) || act.busy} onClick={() => go("security-signoff")}>Security sign-off</button>
            <button disabled={!app.can("plan:approve") || p.status !== "AWAITING_RELEASE" || act.busy} onClick={() => go("release")}>Release environment</button>
            <button disabled={!app.can("run:execute") || !p.has_backup || p.status === "RUNNING" || act.busy} onClick={() => go("rollback")}>Roll back to backup</button>
          </div>
          <p className="muted small">Approvals: {Object.entries(p.approvals).map(([k, v]: J) => `${k} by ${v.by}`).join(", ") || "none yet"}. Creator, executor and agents can never approve, sign off or release.</p>
        </Card>)}
      <Runbook />
    </>
  );
}

function Runbook() {
  const { project } = useApp();
  const [fr, setFr] = useState<J>(null);
  useEffect(() => { setFr(null); if (project) api.get(`/api/full-refresh/plan?source_id=${project.source.id}&target_id=${project.target.id}`).then(setFr).catch(() => undefined); }, [project?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!fr) return null;
  return (
    <Card title="Pair validation for the active project">
      {fr.validation.blockers.map((b: string) => <p key={b}><StatusBadge s="fail" /> {b}</p>)}
      {fr.validation.warnings.map((b: string) => <p key={b}><StatusBadge s="warn" /> {b}</p>)}
      {fr.validation.ok && !fr.validation.warnings.length && <p><StatusBadge s="pass" /> pair is valid for a full refresh</p>}
    </Card>
  );
}
