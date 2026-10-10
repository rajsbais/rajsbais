"""Benchmarks and calibrated duration estimates.

Where samples come from
  * every plan build (extract: rows read from the source and how long it took) and every clean run (mask+stage, load, reconcile)
  * the benchmark harness: scopes of different sizes measured against a source, masked, and loaded into a scratch target
  * imports of measurements taken elsewhere (for example in a controlled test on a real system); these are DECLARED, not verified

Environments keep models apart: 'simulated' (the in-memory simulator on this host), 'platform' (the masking engine on this host),
'remote:<profile>' (an RFC source) or any 'real:<label>' an operator binds to a system. A model never answers for another environment.
Timings of the simulator say nothing about SAP; an estimate over them is marked so.
"""
from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..masking.engine import MaskingEngine, template
from ..sap.adapter import SapSystem
from ..sap.ddic import TABLES
from ..sap.synthetic import SimulatedSap
from ..security.auth import Forbidden, Principal
from ..selective.manifest import Manifest, Scope
from ..service import Conflict, NotFound
from .model import Model, NoModel, fit

PHASES = ("extract", "mask_stage", "load", "reconcile")
RESERVED = {"simulated", "platform"}
SANE_MAX_ROWS_PER_S = 5_000_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Sample:
    id: str
    phase: str
    rows: int
    bytes: int
    seconds: float
    environment: str
    origin: str  # plan | run | harness | import
    label: str = ""
    recorded_at: str = ""
    run_id: str | None = None
    excluded: bool = False
    declared: bool = False  # imported measurements cannot be verified by the platform

    def public(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "phase", "rows", "bytes", "seconds", "environment", "origin", "label", "recorded_at", "run_id", "excluded", "declared")}


class BenchmarkService:
    def __init__(self, svc):
        self.svc = svc
        self.samples: list[Sample] = []
        self.accuracy: list[dict] = []
        self.bindings: dict[str, str] = {}
        self._pending: dict[str, dict] = {}  # project id -> estimate shown for its current plan

    # ------------------------------------------------------------ environments
    def environment_of(self, system_id: str) -> str:
        if system_id in self.bindings:
            return self.bindings[system_id]
        if self.svc.is_local(system_id):
            return "simulated"
        prof = self.svc.remote_profiles.get(system_id)
        return f"remote:{prof.name}" if prof else "remote:unknown"

    def phase_env(self, phase: str, source_id: str, target_id: str | None) -> str:
        return {"extract": self.environment_of(source_id), "mask_stage": "platform"}.get(phase) or (self.environment_of(target_id) if target_id else "simulated")

    def bind(self, actor: Principal, system_id: str, environment: str) -> dict:
        self._human(actor, "system:write")
        self.svc.system(system_id)
        if not environment or environment in RESERVED:
            raise Conflict(f"'{environment}' is reserved; choose a label such as real:QAS-copy-1")
        self.bindings[system_id] = environment
        self.svc.audit.append(actor.id, "benchmark.bound", system_id, {"environment": environment})
        return {"system_id": system_id, "environment": environment}

    @staticmethod
    def _human(actor: Principal, perm: str) -> None:
        if actor.kind != "human" or not actor.can(perm):
            raise Forbidden(f"a human with {perm} is required")

    # ------------------------------------------------------------ recording
    def add(self, phase: str, rows: int, nbytes: int, seconds: float, env: str, origin: str, **kw) -> Sample | None:
        if phase not in PHASES or rows <= 0 or seconds <= 0:
            return None
        s = Sample(f"smp-{uuid.uuid4().hex[:8]}", phase, int(rows), int(nbytes), float(seconds), env, origin, recorded_at=_now().isoformat(), **kw)
        self.samples.append(s)
        return s

    def record_plan(self, project, seconds: float) -> None:
        rows = project.plan.summary()["total_rows"]
        self.add("extract", rows, project.plan.summary()["bytes"], seconds, self.environment_of(project.source_id), "plan", label=project.name)

    def record_run(self, project, run, reconcile_s: float | None) -> None:
        """Samples from a CLEAN run only: retries, failures and resumes include waiting that is not throughput."""
        if run.status != "COMPLETED" or run.attempts != 1:
            return

        def dur(step: str) -> float | None:
            st = next((s for s in run.steps if s["name"] == step), None)
            if not st or not st.get("started") or not st.get("finished"):
                return None
            return (datetime.fromisoformat(st["finished"]) - datetime.fromisoformat(st["started"])).total_seconds()
        env_t = self.environment_of(project.target_id)
        staged = sum(len(r) for rows in run.staged.values() for r in rows.values()) if run.staged else 0
        byt = project.plan.summary()["bytes"]
        if (d := dur("EXTRACT_MASK_STAGE")) and staged:
            self.add("mask_stage", staged, byt, d, "platform", "run", run_id=run.id, label=project.name)
        if (d := dur("LOAD")) and staged:
            self.add("load", staged, byt, d, env_t, "run", run_id=run.id, label=project.name)
        if reconcile_s:
            self.add("reconcile", staged or project.plan.summary()["total_rows"], byt, reconcile_s, env_t, "run", run_id=run.id, label=project.name)
        shown = self._pending.pop(project.id, None)
        if shown and shown["basis"] != "placeholder":
            actual = sum(s.seconds for s in self.samples if s.run_id == run.id) + shown.get("extract_actual", 0.0)
            pred = shown["seconds"]
            self.accuracy.append({"run_id": run.id, "project": project.name, "predicted": round(pred, 4), "low": round(shown["low"], 4), "high": round(shown["high"], 4),
                                  "actual": round(actual, 4), "error": round((pred - actual) / actual, 4) if actual > 0 else None,
                                  "within_interval": shown["low"] <= actual <= shown["high"], "environment": shown["environment"], "at": _now().isoformat()})

    # ------------------------------------------------------------ models
    def usable(self, phase: str, env: str) -> list[Sample]:
        return [s for s in self.samples if s.phase == phase and s.environment == env and not s.excluded]

    def model(self, phase: str, env: str) -> tuple[Model | None, str | None]:
        try:
            return fit([(s.rows, s.seconds) for s in self.usable(phase, env)]), None
        except NoModel as e:
            return None, e.reason

    def models(self) -> list[dict]:
        out = []
        for env in sorted({s.environment for s in self.samples}):
            for ph in PHASES:
                if not self.usable(ph, env):
                    continue
                m, why = self.model(ph, env)
                out.append({"phase": ph, "environment": env, "samples": len(self.usable(ph, env)), "model": m.public() if m else None, "reason": why,
                            "declared": any(s.declared for s in self.usable(ph, env))})
        return out

    # ------------------------------------------------------------ estimates
    def estimate(self, rows: int, nbytes: int, source_id: str, target_id: str | None = None) -> dict:
        from ..agents import advisors
        legacy = advisors.estimate_duration(bytes_total=nbytes, rows_total=rows)
        phases, miss = {}, []
        for ph in PHASES:
            env = self.phase_env(ph, source_id, target_id)
            m, why = self.model(ph, env)
            if m is None:
                miss.append({"phase": ph, "environment": env, "reason": why or "no samples"})
                continue
            phases[ph] = {"environment": env, **m.predict(rows), "quality": m.quality, "n": m.n}
        if len(phases) == len(PHASES):
            total = sum(p["seconds"] for p in phases.values())
            half = sum(p["half_width"] ** 2 for p in phases.values()) ** 0.5  # independence assumed
            envs = {p["environment"] for p in phases.values()}
            return {"basis": "calibrated", "seconds": round(total, 3), "low": round(max(0, total - half), 3), "high": round(total + half, 3), "confidence": "80% prediction interval, phases assumed independent",
                    "phases": {k: {**v, **{x: round(v[x], 4) for x in ("seconds", "low", "high", "half_width")}} for k, v in phases.items()},
                    "extrapolated": any(p["extrapolated"] for p in phases.values()), "quality": max((p["quality"] for p in phases.values()), key=["good", "fair", "poor", "unknown"].index),
                    "environment_note": "simulator timings: they measure this program on this host, not SAP" if envs <= {"simulated", "platform"} else None,
                    "model_assumption": False, "environment": ", ".join(sorted(envs)), "rows": rows, "megabytes": legacy["megabytes"], "missing": [], "placeholder_seconds": legacy["seconds"],
                    "disclaimer": "Calibrated from measured samples with the interval shown; valid for the sizes and environments measured."}
        return {**legacy, "basis": "placeholder", "low": None, "high": None, "phases": phases, "missing": miss, "placeholder_seconds": legacy["seconds"],
                "model_assumption": True,
                "disclaimer": legacy["disclaimer"] + " Not enough measurements for: " + "; ".join(f"{m['phase']} in {m['environment']} ({m['reason']})" for m in miss) + "."}

    def estimate_for_project(self, project) -> dict:
        s = project.plan.summary()
        extract_actual = self.svc.plan_extract_s.get(project.id, 0.0)
        e = self.estimate(s["total_rows"], s["bytes"], project.source_id, project.target_id)
        self._pending[project.id] = {**e, "extract_actual": extract_actual}
        return e

    # ------------------------------------------------------------ import
    def import_sample(self, actor: Principal, spec: dict) -> Sample:
        self._human(actor, "system:write")
        env = str(spec.get("environment", "")).strip()
        if not env or env in RESERVED:
            raise Conflict("environment must name where it was measured, e.g. real:QAS-copy-1 ('simulated' and 'platform' are reserved for measurements the platform takes itself)")
        phase, rows, secs = spec.get("phase"), int(spec.get("rows", 0)), float(spec.get("seconds", 0))
        if phase not in PHASES:
            raise Conflict(f"phase must be one of {PHASES}")
        if rows <= 0 or secs <= 0:
            raise Conflict("rows and seconds must be positive")
        if rows / secs > SANE_MAX_ROWS_PER_S:
            raise Conflict(f"{rows / secs:,.0f} rows/s is not plausible for SAP data movement: check the units")
        s = self.add(phase, rows, int(spec.get("bytes", 0) or 0), secs, env, "import", label=str(spec.get("label", ""))[:80], declared=True)
        self.svc.audit.append(actor.id, "benchmark.imported", s.id, {"phase": phase, "rows": rows, "seconds": secs, "environment": env})
        return s

    def exclude(self, actor: Principal, sid: str, excluded: bool = True) -> Sample:
        self._human(actor, "system:write")
        s = next((x for x in self.samples if x.id == sid), None)
        if s is None:
            raise NotFound(f"sample {sid}")
        s.excluded = excluded
        self.svc.audit.append(actor.id, "benchmark.excluded" if excluded else "benchmark.restored", sid, {})
        return s

    # ------------------------------------------------------------ harness
    def run_benchmark(self, actor: Principal, source_id: str, windows=(15, 30, 60, 90, 120, 180), repeats: int = 2, company_code: str = "1000",
                      clock=time.perf_counter) -> dict:
        """Measure extract (plan build), masking and loading for scopes of growing size, against `source_id` (read-only) and a scratch target."""
        self._human(actor, "run:execute")
        svc = self.svc
        from ..security import authz
        authz.require_systems(svc, actor, source_id)
        sysm = svc.system(source_id)
        reg = svc.registries[sysm.family]
        view = svc.source_view(source_id)
        ref = view.reference_date()
        env_src = self.environment_of(source_id)
        new: list[Sample] = []
        from ..dependency.planner import Planner
        scratch = SimulatedSap(SapSystem(id="bench", sid="BNCH", client="000", role="SBX", product=sysm.product, release=sysm.release),
                               {t: [] for t in TABLES})
        pol = template("gdpr-standard")
        for days in windows:
            for _ in range(max(1, repeats)):
                m = Manifest(name="benchmark", source_system_id=source_id, target_system_id="bench", scope=Scope(object_type="SALES_ORDER", company_codes=[company_code],
                             date_from=ref - timedelta(days=days), date_to=ref), include_downstream=["DELIVERY", "BILLING", "FI_DOCUMENT"])
                t0 = clock()
                plan = Planner(view, reg).build(m)
                t_extract = clock() - t0
                if plan.blocking:
                    continue
                summ = plan.summary()
                rows, byt = summ["total_rows"], summ["bytes"]
                eng = MaskingEngine(pol)
                t0 = clock()
                staged = {iid: {t: [eng.mask_row(t, r) for r in rs] for t, rs in inst.rows.items()} for iid, inst in plan.instances.items()}
                t_mask = clock() - t0
                tgt = SimulatedSap(scratch.system, {t: [] for t in TABLES})
                t0 = clock()
                for iid in plan.order:
                    for t, rs in staged[iid].items():
                        tgt.upsert(t, rs)
                t_load = clock() - t0
                for ph, secs, env in (("extract", t_extract, env_src), ("mask_stage", t_mask, "platform"), ("load", t_load, "simulated")):
                    s = self.add(ph, rows, byt, secs, env, "harness", label=f"{days}d window")
                    if s:
                        new.append(s)
        svc.audit.append(actor.id, "benchmark.run", source_id, {"samples": len(new), "windows": list(windows), "repeats": repeats, "environment": env_src})
        return {"samples_added": len(new), "environment": env_src, "models": self.models()}

    # ------------------------------------------------------------ views
    def summary(self) -> dict:
        errs = [abs(a["error"]) for a in self.accuracy[-20:] if a["error"] is not None]
        cov = [a["within_interval"] for a in self.accuracy[-20:]]
        return {"samples": len(self.samples), "excluded": sum(s.excluded for s in self.samples), "environments": sorted({s.environment for s in self.samples}),
                "bindings": dict(self.bindings), "models": self.models(),
                "accuracy": {"runs": len(self.accuracy), "recent_mape": round(sum(errs) / len(errs), 4) if errs else None,
                             "interval_coverage": round(sum(cov) / len(cov), 3) if cov else None, "history": self.accuracy[-20:][::-1]},
                "notes": ["Timings of the simulator and of the masking engine measure this program on this host, not SAP.",
                          "Imported measurements are declared by the importer and cannot be verified by the platform.",
                          f"A model needs at least 5 samples over 3 different sizes spanning a factor of 2."]}
