"""What is still pending, who it waits on, and the plan to build it. One source: the API serves it, the UI shows it, docs/07-roadmap-tracker.md is generated from it
(python -m rfactory.roadmap --write ../docs/07-roadmap-tracker.md; a test keeps the file in step).

Status:  needs-you  waits on something only the user can do (a machine, a system, a decision)
         ready      can be built now without a real SAP system
         after      waits on another item (see `after`)
         declined   deliberately not built, at the user's instruction
Owner:   you / me / both.   Size: S = under a day, M = a few days, L = a week or more of work, for one person.
"""
from __future__ import annotations

PHASES = [
    ("P1", "Look at it and try it on your machine", "Nothing here needs more building: it needs you to run it."),
    ("P2", "Build what needs no real SAP system", "Can start now and run in parallel while you do P1."),
    ("P3", "Make it true on real systems", "Starts when the smoke-test reports from NPL and A4H arrive."),
    ("P4", "Larger pieces", "Worth doing once P1 to P3 show what matters most."),
]

ITEMS: list[dict] = [
    # ------------------------------------------------------------------------------------------------------------------ P1
    dict(id="R1", phase="P1", title="First run on your PC", area="Verification", owner="you", status="needs-you", size="S", after=[],
         needs="Python 3.11+ and Node.js 20+ on the machine (not on the NPL/A4H VM unless you want to)",
         steps=["Run `python start.py` (Windows: double-click start.bat; this path has never been run on Windows)", "Control tower > Load synthetic landscape, then Guided refresh",
                "Try Data analysis and the Selective designer with a project, a person other than you approving, and Reconciliation", "Tell me what looked wrong or confusing"],
         done="You have seen a refresh released end to end and have a list of anything that looked wrong."),
    dict(id="R2", phase="P1", title="Read-only smoke test against NPL (ECC)", area="Real systems", owner="both", status="needs-you", size="S", after=["R1"],
         needs="NPL running (A4H stopped: only one runs at a time), a read-only SAP user, pyrfc and the SAP NetWeaver RFC SDK installed where the platform runs, the password in an environment variable",
         steps=["Landscape > Connect a real system: fill the form, tick the sandbox/read-only confirmation, run the smoke test", "The test reads a few rows per table and never writes; its report holds no row values",
                "Send me the .md report (docs/04-real-system-test.md has the details)"],
         done="A smoke report from NPL with no unexplained BLOCKER lines."),
    dict(id="R3", phase="P1", title="Read-only smoke test against A4H (S/4HANA)", area="Real systems", owner="both", status="needs-you", size="S", after=["R2"],
         needs="NPL stopped and the A4H appliance in the docker-host VM running; same prerequisites as R2; port 8000 is NPL's, the platform uses 8088",
         steps=["Same as R2 against A4H, over RFC and, if enabled, OData", "Send the report"], done="A smoke report from A4H."),
    dict(id="R10", phase="P1", title="Real identity-provider test of the browser login", area="Security", owner="you", status="needs-you", size="S", after=[],
         needs="A test identity provider (Keycloak, Entra ID, Okta) with an app registration for a public client",
         steps=["Register the app with the redirect URI of the platform", "Set the RFACTORY_OIDC_* variables (README)", "Sign in, sign out, and try a replayed or expired login; send me the result"],
         done="Authorization Code + PKCE login works against a real provider, or the failure is known."),
    # ------------------------------------------------------------------------------------------------------------------ P2
    dict(id="B14", phase="P2", title="Security review pass and supply-chain scanning in CI", area="Security", owner="me", status="ready", size="M", after=[],
         needs="Nothing", steps=["Run a full security review of the API, auth, masking and persistence code and fix what it finds, each with a test", "Add dependency vulnerability scanning and an SBOM to CI",
                                 "Add a secret scan and a container image scan (the image itself is still unbuilt, see R8)"], done="Findings fixed or recorded with a reason; CI fails on a known-vulnerable dependency."),
    dict(id="B2", phase="P2", title="PostgreSQL: connection pool, reconnect and failure drills", area="Persistence", owner="me", status="ready", size="M", after=[],
         needs="Nothing (a local PostgreSQL is available for tests)",
         steps=["Reconnect and retry a request when the database connection drops mid-request, without losing the write lock's guarantees", "A small pool for reads", "Automated drills: database restart, killed connection, killed instance",
                "Document what is guaranteed in each case"], done="Drill tests pass; no lost or duplicated change in any drill."),
    dict(id="B3", phase="P2", title="Schema migrations for the state store", area="Persistence", owner="me", status="ready", size="M", after=[],
         needs="Nothing", steps=["Version every aggregate shape and write forward migrations", "Refuse and explain when a database is newer than the program", "Test upgrading databases saved by earlier versions of this repository"],
         done="An old state database opens, is migrated, and keeps every project and run."),
    dict(id="B7", phase="P2", title="Refresh tokens, silent renewal and token introspection", area="Security", owner="me", status="ready", size="M", after=[],
         needs="Nothing to build; R10 to prove it against a real provider", steps=["Use refresh tokens where the provider issues them and renew before expiry", "Introspect opaque tokens", "Keep the revocation list authoritative"],
         done="A session outlives the access token without re-login; revoked tokens stop at once."),
    dict(id="B13", phase="P2", title="UI: theme switch, in-app help and tour, remaining polish", area="Front end", owner="me", status="ready", size="M", after=["R1"],
         needs="Your notes from R1 decide what to polish first", steps=["A light/dark/auto switch that remembers your choice", "A short guided tour and a help panel on each screen",
                                                                      "Fix what you found in R1", "A screen-reader review needs a person: I will prepare the script, you or a colleague runs it"],
         done="You can find and use every feature without being told where it is."),
    dict(id="B15", phase="P2", title="Operations: TLS reference setup and runbook", area="Operations", owner="me", status="ready", size="M", after=[],
         needs="Nothing", steps=["A reverse proxy with TLS reference (Caddy or nginx) that fits the compose files", "Runbook: backup and restore of the state and its keys, key rotation, upgrade, incident steps",
                                 "Health and metrics endpoints suitable for a monitor"], done="A new operator can run, back up, upgrade and recover it from the document alone."),
    dict(id="B1", phase="P2", title="Delete data tool", area="Data", owner="me", status="declined", size="M", after=[],
         needs="Declined: you asked that no data be deleted", steps=["Not built. The tile stays greyed out as 'Not built yet' in Solutions. Say so if you ever want it."],
         done="Not applicable."),
    # ------------------------------------------------------------------------------------------------------------------ P3
    dict(id="R4", phase="P3", title="Check the table and field models against the real reports", area="Real systems", owner="me", status="after", size="M", after=["R2", "R3"],
         needs="The smoke reports", steps=["Compare each reported table and field with the platform's model for QM, PM, PS, WM, HR, flight and the OData mappings", "Fix names, lengths and keys that differ",
                                           "Record in the capability matrix which models were confirmed on a real system"], done="Every model is marked confirmed or corrected."),
    dict(id="B10", phase="P3", title="Name mapping for the EWM tables (/SCWM/...)", area="Objects", owner="me", status="after", size="S", after=["R4"],
         needs="The real EWM table names from A4H", steps=["Map the SCWM_* stand-ins to the /SCWM/ tables at the connector boundary", "Test against the fake and the real report"], done="An EWM refresh reads the real tables."),
    dict(id="R7", phase="P3", title="Verify the OData mappings on A4H", area="Real systems", owner="me", status="after", size="M", after=["R3"],
         needs="The A4H smoke report with OData enabled", steps=["Compare each mapped entity and property with the service metadata", "Fix gaps; mark what the OData source cannot supply"], done="The OData source reads sales orders correctly or says what it cannot."),
    dict(id="R5", phase="P3", title="Compile and test the ABAP loader in an NPL sandbox", area="Real systems", owner="both", status="after", size="M", after=["R2"],
         needs="A sandbox client in NPL you may write to, developer rights, the ABAP source in docs/abap", steps=["Import and activate ZRF_LOADER and its tables; switch it on in ZRF_CFG for one table", "Run ZRF_PING from the platform",
                                                                                                            "Send me every syntax or runtime error; I fix and you re-run", "Replace the number-range update with the standard number range APIs", "Test upsert, delete and rollback on a dummy table"],
         done="The loader passes its handshake and a write/rollback test in the sandbox."),
    dict(id="R6", phase="P3", title="First real selective refresh into a sandbox", area="Real systems", owner="both", status="after", size="M", after=["R4", "R5"],
         needs="A source and a separate writable sandbox target (for example two NPL clients)", steps=["Read-only plan from the real source", "Approve with a second person", "Run into the sandbox and read the reconciliation", "Roll back and confirm the target is as before"],
         done="A released refresh and a clean rollback on a real system."),
    # ------------------------------------------------------------------------------------------------------------------ P4
    dict(id="R8", phase="P4", title="Build and run the container files; two instances behind a proxy", area="Operations", owner="you", status="needs-you", size="M", after=["B15"],
         needs="Your docker-host VM", steps=["Build the image", "Run docker-compose.postgres.yml with two instances", "Kill one instance mid-request and watch the other", "Send me anything that fails"], done="Both compose files run and survive the drill."),
    dict(id="B5", phase="P4", title="Durable orchestration", area="Orchestration", owner="me", status="ready", size="L", after=["B2"],
         needs="Nothing to build (Temporal or Argo is a product decision for you)", steps=["Move the in-memory queue to the database", "A clock-driven worker with leases enforced inside every module", "Parallel workers with throttling", "Real notification channels"],
         done="Jobs survive restarts and run on time without a person clicking."),
    dict(id="B6", phase="P4", title="ABAP extraction agent for high-volume reads", area="Real systems", owner="me", status="after", size="L", after=["R5"],
         needs="A working loader (R5) so the ABAP side can be compiled", steps=["ABAP source that reads and packages large tables on the SAP side", "Platform adapter and a fake for tests", "Throughput test on a real system"], done="A large table is read without the RFC_READ_TABLE limits."),
    dict(id="B8", phase="P4", title="More business objects", area="Objects", owner="me", status="ready", size="L", after=["R4"],
         needs="Confirmed models (R4) make this safer", steps=["Batches, stock and valuation", "Payroll results and time infotypes (HR)", "Work-centre master", "Key remapping (REMAP) for number clashes", "Each with data, checks, tests and a screen entry"], done="Each object refreshes and reconciles like the existing ones."),
    dict(id="B9", phase="P4", title="Maintenance and project extensions", area="Objects", owner="me", status="ready", size="L", after=["R4"],
         needs="Confirmed models", steps=["Task lists and maintenance plans", "Order components and costs", "Networks, activities, budgets and WBS assignment of orders"], done="Planned maintenance and project cost flows refresh whole."),
    dict(id="B11", phase="P4", title="Data analysis: archiving-object links and aggregation at the source", area="Analysis", owner="me", status="ready", size="M", after=["R4"],
         needs="Real archiving-object definitions from a system", steps=["Map tables to SAP archiving objects", "Push the grouping to the source instead of reading every row"], done="Analyses run on large tables without a row cap."),
    dict(id="B12", phase="P4", title="Multivariate benchmark model and scheduled re-calibration", area="Benchmarks", owner="me", status="ready", size="M", after=["R6"],
         needs="Real timings from R6", steps=["Model row width, index load and parallelism", "Re-fit on a schedule and flag drift"], done="Estimates track real runs within the stated interval."),
    dict(id="B4", phase="P4", title="Key management service providers", area="Security", owner="me", status="ready", size="M", after=[],
         needs="A cloud account to prove it for real", steps=["Providers for AWS KMS and Azure Key Vault behind the existing key interface", "Tested against fakes; real proof is yours"], done="The data key is wrapped by a managed key."),
    dict(id="B17", phase="P4", title="Test-data connectors and a durable catalog", area="Test data", owner="me", status="ready", size="L", after=["B3"],
         needs="Access to Cloud ALM, Jira or Xray to prove it", steps=["Connectors and outbound webhooks", "More templates (PP, FI-AA, MM-IM, intercompany)"], done="A test request raises and closes a ticket automatically."),
    dict(id="B18", phase="P4", title="Delta refresh hardening", area="Refresh", owner="me", status="after", size="L", after=["R6"],
         needs="A real change-document log", steps=["Per-class mapping from configuration", "A scheduler daemon and parallel packages"], done="A weekly delta runs unattended on a real system."),
    dict(id="B16", phase="P4", title="Real full-refresh and post-copy adapters", area="Basis", owner="me", status="after", size="L", after=["R6"],
         needs="Access to SWPM, HANA tools and VM snapshots", steps=["Adapters for the copy tools", "Real adapters for the post-copy tasks"], done="A system copy and its post-copy run from the platform in a sandbox."),
]


def validate() -> list[str]:
    ids = [i["id"] for i in ITEMS]
    problems = []
    if len(ids) != len(set(ids)):
        problems.append("duplicate ids")
    phases = {p[0] for p in PHASES}
    for i in ITEMS:
        if i["phase"] not in phases:
            problems.append(f"{i['id']}: unknown phase")
        if i["status"] not in ("needs-you", "ready", "after", "declined"):
            problems.append(f"{i['id']}: unknown status")
        if i["owner"] not in ("you", "me", "both"):
            problems.append(f"{i['id']}: unknown owner")
        if i["size"] not in ("S", "M", "L"):
            problems.append(f"{i['id']}: unknown size")
        for a in i["after"]:
            if a not in ids:
                problems.append(f"{i['id']}: waits on unknown {a}")
            elif a == i["id"]:
                problems.append(f"{i['id']}: waits on itself")
        if not i["steps"] or not i["done"]:
            problems.append(f"{i['id']}: needs steps and a definition of done")
        if i["status"] == "after" and not i["after"]:
            problems.append(f"{i['id']}: status after but nothing to wait on")
    # no cycles
    graph = {i["id"]: i["after"] for i in ITEMS}
    seen: dict[str, int] = {}

    def visit(n):
        if seen.get(n) == 1:
            problems.append(f"cycle through {n}")
            return
        if seen.get(n) == 2:
            return
        seen[n] = 1
        for m in graph.get(n, []):
            visit(m)
        seen[n] = 2
    for n in graph:
        visit(n)
    return problems


def summary() -> dict:
    by: dict[str, int] = {}
    for i in ITEMS:
        by[i["status"]] = by.get(i["status"], 0) + 1
    return {"total": len(ITEMS), "by_status": by, "by_owner": {o: sum(1 for i in ITEMS if i["owner"] == o) for o in ("you", "me", "both")}}


def public() -> dict:
    return {"phases": [{"id": p, "title": t, "note": n} for p, t, n in PHASES], "items": ITEMS, "summary": summary()}


def markdown() -> str:
    lines = ["# Roadmap tracker: what is still pending and how it will be built", "",
             "Generated from `backend/rfactory/roadmap.py` (do not edit by hand: `python -m rfactory.roadmap --write docs/07-roadmap-tracker.md`). The same list is on the **Roadmap** screen.", "",
             "Status: **needs-you** waits on something only you can do · **ready** can be built now without a real SAP system · **after** waits on another item · **declined** deliberately not built.  Size: S under a day, M a few days, L a week or more.", ""]
    s = summary()
    lines += [f"{s['total']} items: " + ", ".join(f"{v} {k}" for k, v in s["by_status"].items()) + ".", ""]
    for pid, title, note in PHASES:
        lines += [f"## {pid} · {title}", "", f"_{note}_", "", "| ID | Item | Owner | Status | Size | Waits on |", "|---|---|---|---|---|---|"]
        for i in [x for x in ITEMS if x["phase"] == pid]:
            lines.append(f"| {i['id']} | {i['title']} | {i['owner']} | {i['status']} | {i['size']} | {', '.join(i['after']) or '-'} |")
        lines.append("")
        for i in [x for x in ITEMS if x["phase"] == pid]:
            lines += [f"### {i['id']} · {i['title']}", "", f"- **Needs:** {i['needs']}", "- **Plan:**"] + [f"  {n}. {st}" for n, st in enumerate(i["steps"], 1)] + [f"- **Done when:** {i['done']}", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if "--write" in sys.argv:
        path = sys.argv[sys.argv.index("--write") + 1]
        open(path, "w").write(markdown())
        print(f"wrote {path}")
    else:
        print(markdown())
