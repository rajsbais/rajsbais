"""Executable post-copy task library (simulated technical state).

Every task has: prerequisites, SAP version compatibility, pre-check, action, post-check, rollback/recovery text, audit
evidence and an approval requirement. Tasks are idempotent: on an already compliant system the pre-check passes and nothing
changes. Two invariants hold for every task and are re-verified by the run gate:
  * a reference to production is never (re)activated in a non-production system (profile items are validated, restores skip
    anything that points at production, neutralisation only ever sets active=False);
  * what the target keeps is its OWN pre-copy configuration (the approved profile), never something derived from production.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from ..sap.techstate import LISTS, TechState, is_prod_ref, list_items


@dataclass
class Ctx:
    tech: TechState
    profile: dict  # approved pre-copy technical state of the target
    prod_hosts: set[str]
    completed: set[str] = field(default_factory=set)
    mode: str = "deactivate"  # deactivate | delete (for endpoints that have no profile entry)

    def prod(self, item) -> bool:
        return is_prod_ref(item, self.prod_hosts)


@dataclass
class Task:
    id: str
    name: str
    area: str
    categories: tuple[str, ...]
    approval: str  # none | basis_lead | integration_owner | security_officer
    prerequisites: list[str]
    precheck: str
    action: str
    postcheck: str
    rollback: str
    evidence: str
    depends_on: tuple[str, ...] = ()
    families: tuple[str, ...] = ("ECC", "S4")
    min_basis: int = 0
    needs_hana: bool = False
    reversible: bool = True
    order: int = 50

    # --- behaviour (overridden) -------------------------------------------------
    def blocking(self, c: Ctx) -> list[str]:
        """Reasons the task must not run at all (missing prerequisites)."""
        return []

    def violations(self, c: Ctx) -> list[str]:
        """What is wrong right now (empty = compliant)."""
        return []

    def apply(self, c: Ctx) -> None:
        raise NotImplementedError

    # --- shared -----------------------------------------------------------------
    def compatible(self, system) -> tuple[bool, str]:
        if system.family not in self.families:
            return False, f"not applicable to {system.family} (applies to {', '.join(self.families)})"
        if self.needs_hana and "HANA" not in system.db_type.upper():
            return False, "requires a HANA database"
        try:
            if self.min_basis and int(system.release) < self.min_basis:
                return False, f"requires SAP_BASIS >= {self.min_basis} (system has {system.release})"
        except ValueError:
            pass
        return True, "compatible"

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "area": self.area, "versions": [f"{f} (SAP_BASIS >= {self.min_basis or 'any'})" if self.min_basis else f for f in self.families],
                "prerequisites": self.prerequisites, "precheck": self.precheck, "action": self.action, "postcheck": self.postcheck,
                "rollback": self.rollback, "evidence": self.evidence, "approval": self.approval.replace("_", " ") if self.approval != "none" else "none",
                "approval_label": self.approval, "depends_on": list(self.depends_on), "categories": list(self.categories),
                "needs_hana": self.needs_hana, "reversible": self.reversible, "status": "implemented (simulated technical state)",
                "never_reactivate_production_interfaces": True}


# --------------------------------------------------------------------------- generic endpoint tasks
def _profile_items(c: Ctx, cat: str) -> dict[str, dict]:
    idf = LISTS[cat]
    return {it[idf]: it for it in list_items(c.profile, cat)}


def neutralize_violations(c: Ctx, cat: str) -> list[str]:
    idf, want, out = LISTS[cat], _profile_items(c, cat), []
    cur = {it[idf]: it for it in list_items(c.tech.s, cat)}
    for k, it in cur.items():
        if it.get("active", True) and c.prod(it) and k not in want:
            out.append(f"{cat}:{k} is active and points to production ({it.get('host') or it.get('destination') or it.get('env')})")
    for k, d in want.items():
        if cur.get(k) != d:
            out.append(f"{cat}:{k} differs from the approved target profile")
    for k, it in cur.items():
        if k in want and c.prod(it):
            out.append(f"{cat}:{k} points to production")
    return out


def neutralize_apply(c: Ctx, cat: str) -> None:
    idf, want = LISTS[cat], _profile_items(c, cat)
    out, seen = [], set()
    for it in list_items(c.tech.s, cat):
        k = it[idf]
        if k in want:
            out.append(copy.deepcopy(want[k])); seen.add(k)
        elif c.prod(it):
            if c.mode == "delete":
                continue
            dead = {**it, "active": False, "neutralized": True}
            if "trusted" in dead:
                dead["trusted"] = False
            out.append(dead)
        else:
            out.append(it)
    out += [copy.deepcopy(d) for k, d in want.items() if k not in seen]
    c.tech.s[cat] = out


class NeutralizeTask(Task):
    def blocking(self, c: Ctx) -> list[str]:
        bad = [f"{cat}:{k}" for cat in self.categories for k, d in _profile_items(c, cat).items() if c.prod(d)]
        return [f"approved profile contains production reference(s): {', '.join(bad)}"] if bad else []

    def violations(self, c: Ctx) -> list[str]:
        return [v for cat in self.categories for v in neutralize_violations(c, cat)]

    def apply(self, c: Ctx) -> None:
        for cat in self.categories:
            neutralize_apply(c, cat)


# --------------------------------------------------------------------------- specialised tasks
class JobsTask(NeutralizeTask):
    def violations(self, c: Ctx) -> list[str]:
        want = _profile_items(c, "jobs")
        out = []
        for j in c.tech.s["jobs"]:
            live = j["status"] in ("scheduled", "released", "active")
            if live and (j["name"] not in want or c.prod(j)):
                out.append(f"job {j['name']} is {j['status']} but is not part of the target job list" + (" / points to production" if c.prod(j) else ""))
        have = {j["name"]: j for j in c.tech.s["jobs"]}
        out += [f"job {k} from the target job list is missing or changed" for k, d in want.items() if have.get(k) != d]
        return out

    def apply(self, c: Ctx) -> None:
        want, out, seen = _profile_items(c, "jobs"), [], set()
        for j in c.tech.s["jobs"]:
            if j["name"] in want:
                out.append(copy.deepcopy(want[j["name"]])); seen.add(j["name"])
            elif j["status"] in ("scheduled", "released", "active"):
                out.append({**j, "status": "cancelled", "neutralized": True})
            else:
                out.append(j)
        out += [copy.deepcopy(d) for k, d in want.items() if k not in seen]
        c.tech.s["jobs"] = out


class SmtpTask(Task):
    def blocking(self, c):
        return ["approved profile contains a production mail relay"] if c.prod(c.profile["smtp"]) else []

    def violations(self, c):
        s, w = c.tech.s["smtp"], c.profile["smtp"]
        out = []
        if c.prod(s):
            out.append(f"mail relay {s['relay_host']} is a production relay")
        if s != w:
            out.append("SMTP node differs from the approved target profile")
        return out

    def apply(self, c):
        c.tech.s["smtp"] = copy.deepcopy(c.profile["smtp"])


class LogsysTask(Task):
    """BDLS: convert logical-system references from the copied (production) name to the target's own."""

    def blocking(self, c):
        missing = [t for t in self.depends_on if t not in c.completed]
        return [f"prerequisite task(s) not completed: {', '.join(missing)} (jobs must be stopped before BDLS)"] if missing else []

    def violations(self, c):
        want, refs = c.profile["logical_system"], c.tech.s["logsys_refs"]
        out = []
        if c.tech.s["logical_system"] != want:
            out.append(f"client logical system is {c.tech.s['logical_system']}, target expects {want}")
        out += [f"{n} table entries still reference logical system {ls}" for ls, n in refs.items() if ls != want and n > 0]
        return out

    def apply(self, c):
        want = c.profile["logical_system"]
        total = sum(n for ls, n in c.tech.s["logsys_refs"].items())
        old = [ls for ls in c.tech.s["logsys_refs"] if ls != want]
        moved = sum(c.tech.s["logsys_refs"][ls] for ls in old)
        c.tech.s["logsys_refs"] = {want: c.tech.s["logsys_refs"].get(want, 0) + moved}
        c.tech.s["logical_system"] = want
        assert sum(c.tech.s["logsys_refs"].values()) == total  # nothing lost in the conversion


class TmsTask(Task):
    def violations(self, c):
        t, w = c.tech.s["tms"], c.profile["tms"]
        out = [f"TMS domain/controller {t['domain']}@{t['controller']} is not the target's ({w['domain']}@{w['controller']})"] if (t["domain"], t["controller"]) != (w["domain"], w["controller"]) else []
        if not t.get("consistent"):
            out.append("TMS configuration is inconsistent")
        return out

    def apply(self, c):
        c.tech.s["tms"] = copy.deepcopy(c.profile["tms"])


class LicenseTask(Task):
    def blocking(self, c):
        return [] if c.profile["license"].get("valid") else ["no valid target licence in the approved profile"]

    def violations(self, c):
        l, w = c.tech.s["license"], c.profile["license"]
        return [] if l == w and l.get("valid") else [f"licence invalid or not the target's (key {l.get('hardware_key')})"]

    def apply(self, c):
        c.tech.s["license"] = copy.deepcopy(c.profile["license"])


class CertsTask(NeutralizeTask):
    def apply(self, c: Ctx) -> None:
        saved, c.mode = c.mode, "delete"  # production certificates and trusts are removed, never merely deactivated
        try:
            super().apply(c)
        finally:
            c.mode = saved


class UsersTask(Task):
    PRIV = {"SAP_ALL", "SAP_NEW"}

    def violations(self, c):
        want = _profile_items(c, "users")
        out = []
        for u in c.tech.s["users"]:
            if u["user"] in want:
                if u != want[u["user"]]:
                    out.append(f"user {u['user']} differs from the approved target user list")
            elif not u.get("locked"):
                out.append(f"production user {u['user']} ({u['type']}) is not locked")
            elif set(u.get("roles", [])) & self.PRIV:
                out.append(f"locked user {u['user']} still holds {sorted(set(u['roles']) & self.PRIV)}")
        have = {u["user"] for u in c.tech.s["users"]}
        out += [f"approved user {k} is missing" for k in want if k not in have]
        return out

    def apply(self, c):
        want, out, seen = _profile_items(c, "users"), [], set()
        for u in c.tech.s["users"]:
            if u["user"] in want:
                out.append(copy.deepcopy(want[u["user"]])); seen.add(u["user"])
            else:
                out.append({**u, "locked": True, "password_reset_required": True, "roles": [r for r in u.get("roles", []) if r not in self.PRIV], "neutralized": True})
        out += [copy.deepcopy(d) for k, d in want.items() if k not in seen]
        c.tech.s["users"] = out


class HanaTask(Task):
    """Read-only: verifies, never changes HANA internals."""

    def violations(self, c):
        h = c.tech.s["hana"]
        out = []
        if not h.get("backup_catalog_ok"):
            out.append("HANA backup catalog is not healthy")
        if h.get("log_mode") != "normal":
            out.append(f"HANA log mode is {h.get('log_mode')}, expected normal")
        if not h.get("license_ok"):
            out.append("HANA licence is not valid")
        if c.prod(h):
            out.append("HANA state points to a production host")
        return out

    def apply(self, c):
        return None  # verification only: unsupported direct changes to database internals are never made


class ParamsTask(Task):
    def violations(self, c):
        p, w = c.tech.s["params"], c.profile["params"]
        out = [f"parameter {k}={p.get(k)} differs from the target profile ({v})" for k, v in w.items() if p.get(k) != v]
        out += [f"parameter {k} references a production host ({v})" for k, v in p.items() if isinstance(v, str) and v in c.prod_hosts]
        lg, lw = c.tech.s["logon"], c.profile["logon"]
        if lg != lw:
            out.append("logon group / message server differ from the target profile")
        return out

    def apply(self, c):
        c.tech.s["params"] = copy.deepcopy(c.profile["params"])
        c.tech.s["logon"] = copy.deepcopy(c.profile["logon"])


# --------------------------------------------------------------------------- registry
def _mk(cls, **kw):
    return cls(**kw)


TASKS: list[Task] = [
    _mk(JobsTask, id="PC-003", name="Stop and reschedule background jobs", area="batch", categories=("jobs",), approval="none", order=1,
        prerequisites=["job inventory", "approved target job list in the profile"],
        precheck="Active/scheduled/released jobs that are not on the target job list, or that point to production, are listed",
        action="Cancel copied schedules; restore only the target's approved jobs; interface jobs to production stay cancelled",
        postcheck="Only profile jobs are scheduled and none points to production", rollback="Restore the captured job schedule snapshot",
        evidence="Job list before/after diff with counts"),
    _mk(SmtpTask, id="PC-004", name="Deactivate outbound email / SMTP routing", area="notifications", categories=("smtp",), approval="none", order=2,
        prerequisites=["approved sink relay in the profile"], precheck="SCOT node relay is checked against production hosts",
        action="Restore the target's sink relay; outbound mail disabled per profile", postcheck="Relay is not production; simulated test mail lands on the sink",
        rollback="Restore captured SCOT node", evidence="SCOT node before/after + test-mail delivery host"),
    _mk(NeutralizeTask, id="PC-002", name="Neutralize outbound RFC destinations", area="rfc", categories=("rfc",), approval="integration_owner", order=3,
        prerequisites=["approved RFC destinations in the profile"], precheck="SM59 destinations pointing at production hosts are listed",
        action="Restore target destinations; deactivate (or delete) anything else that points to production", postcheck="No active RFC destination resolves to a production host",
        rollback="Restore captured SM59 snapshot", evidence="Before/after RFC destination diff"),
    _mk(NeutralizeTask, id="PC-005", name="Disable IDoc partner profiles and ports to production partners", area="idoc", categories=("idoc",), approval="integration_owner", order=4,
        prerequisites=["approved partner profiles in the profile"], precheck="WE20 partners/ports pointing at production are listed",
        action="Restore target partners; deactivate production partners and ports", postcheck="No IDoc leaves the target towards production",
        rollback="Restore captured WE20/WE21 snapshot", evidence="Partner profile diff"),
    _mk(NeutralizeTask, id="PC-015", name="Detach external batch-scheduler integrations", area="batch", categories=("schedulers",), approval="integration_owner", order=5,
        prerequisites=["approved scheduler agents in the profile"], precheck="External scheduler agents (XBP/Control-M style) registered with production schedulers are listed",
        action="Restore the target's agent registration; deactivate production scheduler links", postcheck="No active link to a production scheduler",
        rollback="Restore captured agent registration", evidence="Scheduler integration diff"),
    _mk(NeutralizeTask, id="PC-012", name="Redirect printers and spool destinations", area="spool", categories=("printers",), approval="none", order=6,
        prerequisites=["approved output devices in the profile"], precheck="Output devices pointing at production print hosts are listed",
        action="Restore target devices; deactivate production devices", postcheck="No active output device targets a production print host",
        rollback="Restore captured SPAD snapshot", evidence="Output device diff"),
    _mk(NeutralizeTask, id="PC-013", name="Repoint monitoring and alert targets", area="monitoring", categories=("monitoring",), approval="none", order=7,
        prerequisites=["approved monitoring targets in the profile"], precheck="Monitoring/alerting targets that report to production tooling are listed",
        action="Restore the target's monitoring registration; deactivate production alert targets", postcheck="No active alert target in production",
        rollback="Restore captured monitoring configuration", evidence="Monitoring target diff"),
    _mk(NeutralizeTask, id="PC-010", name="Cloud destinations", area="cloud", categories=("cloud",), approval="integration_owner", order=8,
        prerequisites=["approved non-production tenants in the profile"], precheck="Production tenant URLs are listed",
        action="Restore non-production tenants; deactivate production tenants", postcheck="No active destination points to a production tenant",
        rollback="Restore captured destination snapshot", evidence="Destination diff"),
    _mk(NeutralizeTask, id="PC-016", name="PI/PO and Integration Suite connectivity", area="integration", categories=("integration",), approval="integration_owner", order=9,
        prerequisites=["approved integration endpoints in the profile"], precheck="Sender/receiver channels and tenant endpoints that target production are listed",
        action="Restore test endpoints; deactivate production channels and tenants", postcheck="No active integration endpoint targets production",
        rollback="Restore captured channel/endpoint configuration", evidence="Endpoint diff"),
    _mk(NeutralizeTask, id="PC-017", name="Gateway / Fiori system aliases", area="gateway", categories=("gateway",), approval="integration_owner", order=10,
        families=("S4",), min_basis=750, prerequisites=["approved system aliases in the profile"], precheck="System aliases routing to production back ends are listed",
        action="Restore target aliases; deactivate production aliases", postcheck="No active alias routes to a production back end",
        rollback="Restore captured alias snapshot", evidence="Alias diff"),
    _mk(CertsTask, id="PC-008", name="Reset SSO, trust relationships and certificates", area="security", categories=("certs", "sso_trusts"), approval="security_officer", order=11,
        prerequisites=["approved PSEs and trusted systems in the profile"], precheck="Production certificates and trusts are listed",
        action="Remove production certificates and trusts; restore the target's", postcheck="No production certificate or trust remains",
        rollback="Restore captured STRUST export", evidence="Certificate/trust inventory diff"),
    _mk(UsersTask, id="PC-009", name="Lock and reset copied production users per policy", area="security", categories=("users",), approval="security_officer", order=12,
        reversible=False, prerequisites=["approved target user list in the profile"], precheck="Production users that are not on the target list are listed",
        action="Lock every user not on the target list, force password reset, remove SAP_ALL/SAP_NEW", postcheck="No unlocked production user and no SAP_ALL outside the target list",
        rollback="Restore captured user snapshot (in a real system: restore USR02 from the pre-change backup)", evidence="User policy report: counts only, no credentials"),
    _mk(LicenseTask, id="PC-007", name="Install SAP licence", area="license", categories=("license",), approval="none", order=13,
        prerequisites=["valid target licence in the profile"], precheck="SLICENSE status and hardware key", action="Install the target licence", postcheck="Licence valid for the target hardware key",
        rollback="Reinstall the temporary licence", evidence="Licence status"),
    _mk(TmsTask, id="PC-006", name="Reconfigure TMS / transport routes", area="tms", categories=("tms",), approval="basis_lead", order=14,
        prerequisites=["TMS configuration of the target in the profile"], precheck="Domain, controller and consistency", action="Restore the target's transport configuration",
        postcheck="STMS consistency check green and domain is the target's", rollback="Re-import captured STMS configuration", evidence="STMS check output"),
    _mk(ParamsTask, id="PC-014", name="Instance profile parameters and logon groups", area="profile", categories=("params", "logon"), approval="basis_lead", order=15,
        prerequisites=["target parameters in the profile"], precheck="Parameters/logon groups that reference production hosts", action="Restore the target's parameters and logon group",
        postcheck="No parameter references a production host", rollback="Restore captured profile parameters", evidence="Parameter diff"),
    _mk(LogsysTask, id="PC-001", name="Set logical system names (BDLS)", area="logical systems", categories=("logical_system", "logsys_refs"), approval="basis_lead", order=16,
        depends_on=("PC-003", "PC-002"), reversible=False, prerequisites=["target logical system in the profile", "jobs stopped (PC-003)", "RFC neutralised (PC-002)"],
        precheck="Client logical system and residual references to the source logical system",
        action="Convert all references from the copied logical system to the target's (BDLS); client logical system set", postcheck="No entry references the source logical system",
        rollback="Re-run BDLS with the reverse mapping from the captured counts (in a real system BDLS is not trivially reversible: recover from backup)",
        evidence="BDLS conversion counts before/after"),
    _mk(HanaTask, id="PC-011", name="HANA technical checks", area="hana", categories=("hana",), approval="none", order=17, needs_hana=True, depends_on=("PC-007",),
        prerequisites=[], precheck="Backup catalog, log mode, licence", action="Verify only: no changes to HANA internals are ever made", postcheck="All checks green",
        rollback="n/a (read-only)", evidence="Check output"),
]
BY_ID = {t.id: t for t in TASKS}


def catalog() -> list[dict]:
    return [t.describe() for t in sorted(TASKS, key=lambda t: t.id)]


def ordered(selected: list[str] | None) -> list[Task]:
    """Selected tasks (default: all) plus their dependencies, ordered by dependency then by `order`."""
    want = set(selected or BY_ID)
    unknown = want - set(BY_ID)
    if unknown:
        raise KeyError(f"unknown task(s): {sorted(unknown)}")
    grew = True
    while grew:
        grew = False
        for t in list(want):
            for d in BY_ID[t].depends_on:
                if d not in want:
                    want.add(d); grew = True
    out, done = [], set()
    pending = sorted((BY_ID[i] for i in want), key=lambda t: t.order)
    while pending:
        nxt = next(t for t in pending if all(d in done or d not in want for d in t.depends_on))
        out.append(nxt); done.add(nxt.id); pending.remove(nxt)
    return out
