import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, ErrorNote, Stat, useAction } from "../components";

const STATUS: Record<string, { label: string; kind: string; hint: string }> = {
  "needs-you": { label: "Needs you", kind: "warn", hint: "waits on something only you can do" },
  ready: { label: "Ready to build", kind: "ok", hint: "can be built now without a real SAP system" },
  after: { label: "Waiting on another item", kind: "info", hint: "starts when the items it waits on are done" },
  declined: { label: "Declined", kind: "", hint: "deliberately not built, at your instruction" },
};
const OWNER: Record<string, string> = { you: "You", me: "Claude", both: "You and Claude" };
const SIZE: Record<string, string> = { S: "under a day", M: "a few days", L: "a week or more" };

export default function Roadmap() {
  const [data, setData] = useState<J>(null);
  const [status, setStatus] = useState("all");
  const [owner, setOwner] = useState("all");
  const act = useAction();
  useEffect(() => { void act.run(async () => setData(await api.get("/api/roadmap"))); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (!data) return <Card title="Roadmap"><ErrorNote error={act.error} /><p className="muted">Loading…</p></Card>;
  const items: J[] = data.items;
  const shown = items.filter((i) => (status === "all" || i.status === status) && (owner === "all" || i.owner === owner));
  const title = (id: string) => items.find((x) => x.id === id)?.title ?? id;
  const s = data.summary;
  return (
    <>
      <Card title="What is still pending">
        <p className="muted">Everything that is not done yet, who it waits on, and the plan to build it. Done work is in the capability matrix (Control tower); this list is only what remains.</p>
        <div className="grid stats">
          <Stat label="Items" value={s.total} />
          <Stat label="Need you" value={s.by_status["needs-you"] ?? 0} hint="a machine, a system or a decision" />
          <Stat label="Ready to build" value={s.by_status.ready ?? 0} hint="no real SAP system needed" />
          <Stat label="Waiting" value={s.by_status.after ?? 0} hint="on another item" />
        </div>
        <div className="row" role="group" aria-label="Filter by status">
          {[["all", "All"], ...Object.entries(STATUS).map(([k, v]) => [k, v.label])].map(([k, l]) => (
            <button key={k} className={status === k ? "primary" : ""} aria-pressed={status === k} onClick={() => setStatus(k)}>{l}</button>))}
        </div>
        <div className="row" role="group" aria-label="Filter by owner">
          {[["all", "Anyone"], ["you", "You"], ["me", "Claude"], ["both", "Both"]].map(([k, l]) => (
            <button key={k} className={owner === k ? "primary" : ""} aria-pressed={owner === k} onClick={() => setOwner(k)}>{l}</button>))}
        </div>
        <p className="muted small" aria-live="polite">{shown.length} of {items.length} item(s) shown</p>
      </Card>
      {data.phases.map((ph: J) => {
        const inPhase = shown.filter((i) => i.phase === ph.id);
        if (!inPhase.length) return null;
        return (
          <Card key={ph.id} title={`${ph.id} · ${ph.title}`}>
            <p className="muted small">{ph.note}</p>
            <ul className="plain">
              {inPhase.map((i) => (
                <li key={i.id}>
                  <details className="rec">
                    <summary><strong>{i.id}</strong> · {i.title}{" "}
                      <Badge kind={STATUS[i.status].kind}>{STATUS[i.status].label}</Badge> <Badge>{OWNER[i.owner]}</Badge> <Badge>{i.size}: {SIZE[i.size]}</Badge></summary>
                    <p><strong>Needs:</strong> {i.needs}</p>
                    {i.after.length > 0 && <p><strong>Waits on:</strong> {i.after.map((a: string) => `${a} (${title(a)})`).join("; ")}</p>}
                    <p><strong>Plan:</strong></p>
                    <ol>{i.steps.map((st: string, n: number) => <li key={n}>{st}</li>)}</ol>
                    <p><strong>Done when:</strong> {i.done}</p>
                  </details>
                </li>))}
            </ul>
          </Card>);
      })}
    </>
  );
}
