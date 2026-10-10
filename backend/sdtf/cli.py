from __future__ import annotations

import argparse
import json
import sys

from .db import init_schema, session_scope
from .security.auth import seed_dev_users


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sdtf", description="SAP Selective Data Transformation Factory CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create schema and seed dev users")
    d = sub.add_parser("demo", help="run the vertical slice end to end on a synthetic landscape")
    d.add_argument("--scale", type=int, default=1)
    d.add_argument("--seed", type=int, default=42)
    d.add_argument("--json", action="store_true")
    d.add_argument("--connector", choices=["SYNTHETIC", "RFC"], default="SYNTHETIC", help="RFC runs extraction through the simulated SAP add-on over the RFC adapter")
    w = sub.add_parser("worker", help="run a distributed extraction worker (claims partition jobs)")
    w.add_argument("--worker-id", default=None)
    w.add_argument("--until-idle", action="store_true", help="exit when no job is available")
    w.add_argument("--max-jobs", type=int, default=None)
    w.add_argument("--lease-seconds", type=int, default=None)
    s = sub.add_parser("serve", help="run the API server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    f = sub.add_parser("fake-idp", help="run the TEST-ONLY OpenID Connect provider for local PKCE login demos")
    f.add_argument("--host", default="127.0.0.1")
    f.add_argument("--port", type=int, default=9400)
    f.add_argument("--issuer", default=None)
    c = sub.add_parser("cockpit-export", help="write the migration cockpit staging files (CSV + SpreadsheetML) of a completed run")
    c.add_argument("--run", required=True, help="run id")
    c.add_argument("--out", default=None, help="output directory (default: <evidence dir>/cockpit)")
    c.add_argument("--format", choices=["csv", "xml", "both"], default="both")
    c.add_argument("--scope", choices=["all", "rejected"], default="all", help="rejected: retry package of the instances the imported simulation feedback rejected")
    c.add_argument("--json", action="store_true")
    fb = sub.add_parser("cockpit-feedback", help="upload simulation feedback of the migration cockpit: import the app's message log, show the summary, write an illustrative sample")
    fbsub = fb.add_subparsers(dest="fcmd", required=True)
    fi = fbsub.add_parser("import", help="import a message log (CSV/TSV, JSON or SpreadsheetML)")
    fi.add_argument("--run", required=True)
    fi.add_argument("--file", required=True)
    fi.add_argument("--append", action="store_true", help="keep earlier feedback of the round")
    fi.add_argument("--round", type=int, default=None, help="the round the log answers (default: latest round not yet simulated)")
    fi.add_argument("--json", action="store_true")
    fr = fbsub.add_parser("rounds", help="package rounds of the run: statuses, results, burn-down of the still-rejected instances")
    fr.add_argument("--run", required=True)
    fr.add_argument("--json", action="store_true")
    fm = fbsub.add_parser("mark", help="record by hand that a round was uploaded in the app or migrated")
    fm.add_argument("--run", required=True)
    fm.add_argument("--round", type=int, required=True)
    fm.add_argument("--status", choices=["UPLOADED", "MIGRATED"], required=True)
    fm.add_argument("--note", default="")
    fs = fbsub.add_parser("show", help="summary and messages of the run's feedback")
    fs.add_argument("--run", required=True)
    fs.add_argument("--json", action="store_true")
    fsa = fbsub.add_parser("sample", help="write an illustrative message log for the run (not an SAP file)")
    fsa.add_argument("--run", required=True)
    fsa.add_argument("--out", default=None)
    t = sub.add_parser("cockpit-template", help="migration object templates: write an illustrative sample, register a template for a project, list a project's templates")
    tsub = t.add_subparsers(dest="tcmd", required=True)
    ts = tsub.add_parser("sample", help="write an illustrative template (not an SAP file) for a business object")
    ts.add_argument("--object", required=True)
    ts.add_argument("--out", default=None, help="file to write (default: stdout)")
    tr = tsub.add_parser("register", help="register a template file for a project and business object")
    tr.add_argument("--project", required=True)
    tr.add_argument("--object", required=True)
    tr.add_argument("--file", required=True)
    tr.add_argument("--mapping", default=None, help="JSON file with explicit overrides")
    tr.add_argument("--json", action="store_true")
    tl = tsub.add_parser("list", help="list a project's templates with their mapping reports")
    tl.add_argument("--project", required=True)
    ta = tsub.add_parser("aliases", help="project aliases learned from template Field Lists: list, propose from the registered templates, confirm/reject")
    ta.add_argument("--project", required=True)
    ta.add_argument("--propose", action="store_true", help="re-read the registered templates and record proposals")
    ta.add_argument("--confirm", default=None, help="comma-separated alias names (or 'all') to confirm")
    ta.add_argument("--reject", default=None, help="comma-separated alias names to reject")
    ta.add_argument("--json", action="store_true")
    tc = tsub.add_parser("check", help="check a downloaded template file against the documented layout (no database needed)")
    tc.add_argument("--file", required=True)
    tc.add_argument("--object", default=None, help="business object: also print the automatic mapping report")
    tc.add_argument("--json", action="store_true")
    cr = sub.add_parser("cutover-rehearsal", help="cutover rehearsal checklist: create a mock cutover / dress rehearsal / go-live checklist for a manifest, refresh the automatic items, tick manual items, time runbook tasks, record lessons, complete with GO / NO_GO, export the report")
    crsub = cr.add_subparsers(dest="rcmd", required=True)
    crc = crsub.add_parser("create", help="create a rehearsal for a manifest (automatic items evaluated now)")
    crc.add_argument("--manifest", required=True)
    crc.add_argument("--name", default="")
    crc.add_argument("--kind", choices=["MOCK", "DRESS", "FINAL"], default="MOCK")
    crc.add_argument("--json", action="store_true")
    crl = crsub.add_parser("list", help="rehearsals of a manifest")
    crl.add_argument("--manifest", required=True)
    crl.add_argument("--json", action="store_true")
    crs = crsub.add_parser("show", help="checklist, timings and lessons of a rehearsal")
    crs.add_argument("--id", required=True)
    crs.add_argument("--json", action="store_true")
    crr = crsub.add_parser("refresh", help="re-evaluate the automatic items from the platform state")
    crr.add_argument("--id", required=True)
    crr.add_argument("--json", action="store_true")
    crst = crsub.add_parser("start", help="start the rehearsal (tasks can then be timed)")
    crst.add_argument("--id", required=True)
    crst.add_argument("--note", default="")
    crm = crsub.add_parser("mark", help="tick a manual item (or waive an automatic one with NOT_APPLICABLE and a note)")
    crm.add_argument("--id", required=True)
    crm.add_argument("--item", required=True)
    crm.add_argument("--status", required=True, choices=["PENDING", "PASS", "FAIL", "NOT_APPLICABLE"])
    crm.add_argument("--note", default="")
    crt = crsub.add_parser("task", help="record that a runbook task started or finished")
    crt.add_argument("--id", required=True)
    crt.add_argument("--task", required=True)
    crt.add_argument("--action", required=True, choices=["start", "finish"])
    crt.add_argument("--note", default="")
    crle = crsub.add_parser("lesson", help="record a lesson learned")
    crle.add_argument("--id", required=True)
    crle.add_argument("--text", required=True)
    crle.add_argument("--task", default="")
    crco = crsub.add_parser("complete", help="the approver's verdict (GO is refused while a blocking item is open)")
    crco.add_argument("--id", required=True)
    crco.add_argument("--verdict", required=True, choices=["GO", "NO_GO"])
    crco.add_argument("--note", default="")
    crab = crsub.add_parser("abort", help="abort the rehearsal")
    crab.add_argument("--id", required=True)
    crab.add_argument("--note", default="")
    crre = crsub.add_parser("report", help="write the checklist report (Markdown)")
    crre.add_argument("--id", required=True)
    crre.add_argument("--out", default=None)
    crtl = crsub.add_parser("timeline", help="live execution: every runbook task against the clock, downtime clock, incidents, assignments")
    crtl.add_argument("--id", required=True)
    crtl.add_argument("--json", action="store_true")
    cras = crsub.add_parser("assign", help="assign a runbook task to a person or role (empty assignee clears it)")
    cras.add_argument("--id", required=True)
    cras.add_argument("--task", required=True)
    cras.add_argument("--assignee", default="")
    cras.add_argument("--backup", default="")
    cras.add_argument("--contact", default="")
    crin = crsub.add_parser("incident", help="raise, escalate or resolve an incident on a runbook task")
    crin.add_argument("--id", required=True)
    crin.add_argument("--action", required=True, choices=["raise", "escalate", "resolve"])
    crin.add_argument("--task", default="", help="raise: the runbook task")
    crin.add_argument("--severity", default="MEDIUM", choices=["LOW", "MEDIUM", "HIGH", "CRITICAL"])
    crin.add_argument("--title", default="")
    crin.add_argument("--detail", default="")
    crin.add_argument("--incident", default="", help="escalate / resolve: the incident id")
    crin.add_argument("--to", default="", help="escalate: a named person or role (default: next level of the owner's path)")
    crin.add_argument("--note", default="", help="escalate: note; resolve: the resolution")
    bch = sub.add_parser("bench", help="benchmark harness: run the vertical slice at the given scales on the simulators, time every step and rewrite the measured section of docs/benchmarks.md")
    bch.add_argument("--scales", default="1", help="comma-separated scales, e.g. 1,2,3")
    bch.add_argument("--seed", type=int, default=42)
    bch.add_argument("--workers", type=int, default=4)
    bch.add_argument("--out", default=None, help="Markdown document whose measured section is replaced (the JSON is written beside it)")
    bch.add_argument("--json", action="store_true")
    dsc = sub.add_parser("discover", help="discover a registered system: through the read-only add-on (RFC sources) or from the platform's record store")
    dsc.add_argument("--system", required=True, help="registered system id")
    dsc.add_argument("--path", choices=["rfc", "record_store"], default=None, help="override the read path (default: rfc for RFC sources, record_store otherwise)")
    dsc.add_argument("--sample", type=int, default=None, help="rfc path: read only the first N instances per business object (quick look; the scope engine refuses a sampled discovery)")
    dsc.add_argument("--json", action="store_true")
    bpa = sub.add_parser("process-analysis", help="business process analysis from the database footprint of a registered system (TAANA-style variants, DB05 selectivity, age, DB02 growth, DB15 links) through the add-on")
    bpa.add_argument("--system", required=True, help="registered system id")
    bpa.add_argument("--area", choices=["ALL", "O2C", "P2P", "R2R"], default="ALL")
    bpa.add_argument("--bukrs", default=None, help="comma-separated company codes for the pushdown")
    bpa.add_argument("--top", type=int, default=10)
    bpa.add_argument("--retention-years", type=int, default=7)
    bpa.add_argument("--json", action="store_true")
    bpa.add_argument("--out", default=None, help="write the Markdown report here")
    lk = sub.add_parser("lookup-csv", help="turn a two-column CSV export into a lookup table of the rule DSL (duplicates and conflicts reported); paste the YAML into a rule set")
    lk.add_argument("--file", required=True, help="CSV file (comma, semicolon, tab or pipe)")
    lk.add_argument("--name", required=True, help="lookup name, e.g. coa_map")
    lk.add_argument("--key-column", default=None)
    lk.add_argument("--value-column", default=None)
    lk.add_argument("--json", action="store_true", help="print the parse report instead of the YAML")
    mdp = sub.add_parser("metadata", help="verify the API bindings against a service's $metadata: from a downloaded EDMX file or fetched from a registered API target")
    mdsub = mdp.add_subparsers(dest="mdcmd", required=True)
    mdc = mdsub.add_parser("check", help="check one service against an EDMX file, or every bound service against a target")
    mdc.add_argument("--file", default=None, help="EDMX ($metadata) file")
    mdc.add_argument("--service", default=None, help="service name the file belongs to (e.g. API_JOURNALENTRYITEMBASIC_SRV)")
    mdc.add_argument("--system", default=None, help="registered API target to fetch the catalogue and $metadata from")
    mdc.add_argument("--url", default=None, help="base URL of an S/4HANA system to check directly, no registration needed (e.g. https://vhcala4hci.dummy.nodomain:44300)")
    mdc.add_argument("--user", default=None, help="user for --url (basic authentication)")
    mdc.add_argument("--passwd-env", default="SDTF_METADATA_PASSWD", help="environment variable holding the password for --url (never passed on the command line)")
    mdc.add_argument("--no-verify", action="store_true", help="do not verify the TLS certificate (lab systems with self-signed certificates only)")
    mdc.add_argument("--services", default=None, help="comma-separated services to check (default: every bound service)")
    mdc.add_argument("--out", default=None, help="write the Markdown report here")
    mdc.add_argument("--json", action="store_true")
    mde = mdsub.add_parser("expectations", help="list the entity sets and properties the platform relies on per service")
    mde.add_argument("--service", default=None)
    mde.add_argument("--json", action="store_true")
    rc = sub.add_parser("reconcile", help="re-run the three-layer reconciliation of a completed run through the adapters (source over the RFC add-on, target over the released APIs)")
    rc.add_argument("--run", required=True)
    rc.add_argument("--mode", choices=["auto", "rows", "aggregate"], default="auto", help="rows: read the rows through the adapters; aggregate: totals computed in the source and, with the add-on on the target, in the target; auto: per side by journal size")
    rc.add_argument("--json", action="store_true")
    mo = sub.add_parser("migration-objects", help="migration object lookup per S/4HANA release: list the catalogue, resolve a business object, import the target's object list for a project")
    mosub = mo.add_subparsers(dest="mcmd", required=True)
    ml = mosub.add_parser("list", help="catalogue (or the project's resolution table) for a release")
    ml.add_argument("--release", default=None)
    ml.add_argument("--project", default=None, help="resolve for this project's registry and target release")
    ml.add_argument("--object", default="", help="business object to resolve")
    ml.add_argument("--json", action="store_true")
    mi = mosub.add_parser("import", help="import entries [{name, id, release, object_types, tables, notes}] from a JSON file")
    mi.add_argument("--project", required=True)
    mi.add_argument("--file", required=True)
    mi.add_argument("--release", default=None)
    mi.add_argument("--replace", action="store_true")
    mi.add_argument("--source", default="")
    a = ap.parse_args(argv)
    if a.cmd == "fake-idp":
        from .security.fake_idp import main as fake_idp_main

        return fake_idp_main(["--host", a.host, "--port", str(a.port)] + (["--issuer", a.issuer] if a.issuer else []))
    init_schema()
    if a.cmd != "worker":  # workers never seed identities
        with session_scope() as session:
            seed_dev_users(session)
    if a.cmd == "init-db":
        print("schema ready; dev users seeded")
        return 0
    if a.cmd == "demo":
        from .demo import run_vertical_slice

        with session_scope() as session:
            out = run_vertical_slice(session, scale=a.scale, seed=a.seed, connector=a.connector)
            run = out["run"]
            if a.json:
                print(json.dumps({"project_id": out["project"].id, "manifest_id": out["manifest"].id, "run_id": run.id, "status": run.status, "reconciliation": run.report["reconciliation"]}, indent=2, default=str))
            else:
                print(run.report["markdown"])
                print(f"project_id={out['project'].id} manifest_id={out['manifest'].id} run_id={run.id}")
        return 0
    if a.cmd == "migration-objects":
        from .catalog.migration_objects import catalogue, lookup_table

        if a.mcmd == "list" and not a.project:
            rows = lookup_table(a.object, a.release) if a.object else None
            if a.json:
                print(json.dumps(rows if rows is not None else catalogue(a.release), indent=2, default=str))
            elif rows is not None:
                for ot, r in rows.items():
                    print(f"{ot}: {r['status']} {r['name']} [{r['id'] or '-'}] ({r['source']}, {r['confidence']}) {r['note']}")
            else:
                for c in catalogue(a.release):
                    print(f"{c['key']:24} {c['name'] or '(not available in ' + str(a.release) + ')':45} id hint {c['id_hint']:24} for {', '.join(c['object_types']) or '-'}")
            return 0
        from .runtime.migration_objects import entry_out, import_entries, project_lookup

        with session_scope() as session:
            if a.mcmd == "import":
                entries = json.load(open(a.file, encoding="utf-8"))
                if isinstance(entries, dict):
                    entries = entries.get("entries", [])
                try:
                    rows = import_entries(session, a.project, entries, "cli", a.release, a.replace, a.source)
                except ValueError as e:
                    print(str(e), file=sys.stderr)
                    return 2
                session.commit()
                print(f"imported {len(rows)} migration object(s) for project {a.project}")
                for e in rows:
                    print(f"  {e.release:6} {e.name} [{e.object_id or '-'}] -> {', '.join(e.object_types or []) or '-'}")
                return 0
            res = project_lookup(session, a.project, a.release, a.object)
            if a.json:
                print(json.dumps(res, indent=2, default=str))
            else:
                print(f"project {a.project}, release {res['release'] or '-'} ({res['normalized_release'] or 'unknown'}), {res['registry_entries']} registry entries")
                for ot, r in res["objects"].items():
                    print(f"  {ot}: {r['status']} {r['name']} [{r['id'] or '-'}] ({r['source']}, {r['confidence']}) {r['note']}")
            _ = entry_out
        return 0
    if a.cmd == "cockpit-template":
        from .runtime.cockpit_templates import (
            register_template,
            sample_template,
            template_summary,
            templates_for,
        )

        if a.tcmd == "check":
            from .runtime.cockpit_templates import auto_map, check_template, mapping_report, parse_template

            with open(a.file, encoding="utf-8") as fh:
                content = fh.read()
            chk = check_template(content)
            rep = mapping_report(auto_map(parse_template(content), a.object)) if chk["ok"] and a.object else None
            if a.json:
                print(json.dumps({"check": chk, "mapping": rep}, indent=2, default=str))
            elif not chk["ok"]:
                print(f"not a usable template: {chk['error']}", file=sys.stderr)
            else:
                print(f"{a.file}: documented layout = {chk['documented_layout']}; sheets {chk['sheet_order']}; field list {chk['field_list']} (technical names in {chk['field_list_columns'].get('technical_names_in', '-')})")
                for sh in chk["sheets"]:
                    print(f"  {sh['name']}: {sh['fields']} fields, {sh['header_rows']} header rows, {sh['key_columns']} key column(s), structure {sh['structure'] or '-'}, rows found {sh['signals']}")
                for w in chk["warnings"]:
                    print(f"  WARNING {w}")
                if rep:
                    print(f"  mapping for {a.object}: {rep['mapped']}/{rep['total']} fields; mandatory unmapped: {rep['mandatory_missing'] or 'none'}; sheets without table: {rep['unmapped_sheets'] or 'none'}")
            return 0 if chk["ok"] else 2
        if a.tcmd == "sample":
            try:
                xml = sample_template(a.object)
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 2
            if a.out:
                with open(a.out, "w", encoding="utf-8") as fh:
                    fh.write(xml)
                print(f"illustrative template for {a.object} written to {a.out}")
            else:
                print(xml)
            return 0
        with session_scope() as session:
            if a.tcmd == "aliases":
                from sqlalchemy import select

                from .models import CockpitAlias
                from .runtime.cockpit_templates import alias_out, decide_alias, store_proposals

                if a.propose:
                    for _, row in sorted(templates_for(session, a.project).items()):
                        store_proposals(session, a.project, row, "cli")
                rows = session.execute(select(CockpitAlias).where(CockpitAlias.project_id == a.project).order_by(CockpitAlias.status, CockpitAlias.alias)).scalars().all()
                for which, status in ((a.confirm, "CONFIRMED"), (a.reject, "REJECTED")):
                    if which:
                        names = None if which.strip().lower() == "all" else {x.strip().upper() for x in which.split(",")}
                        for r in rows:
                            if r.status == "PROPOSED" and (names is None or r.alias in names):
                                decide_alias(session, r, status, "cli")
                session.commit()
                if a.json:
                    print(json.dumps([alias_out(r) for r in rows], indent=2, default=str))
                else:
                    for r in rows:
                        print(f"{r.status:9} {r.alias} -> {r.table_name + '.' if r.table_name else ''}{r.field} ({r.description}) [{r.evidence}]")
                    if not rows:
                        print("no aliases recorded for this project (register a template, or run with --propose)")
                return 0
            if a.tcmd == "register":
                with open(a.file, encoding="utf-8") as fh:
                    content = fh.read()
                mapping = json.load(open(a.mapping, encoding="utf-8")) if a.mapping else None
                try:
                    row = register_template(session, a.project, a.object, content, a.file.rsplit("/", 1)[-1], "cli", mapping)
                except ValueError as e:
                    print(str(e), file=sys.stderr)
                    return 2
                from .runtime.cockpit_templates import project_aliases, store_proposals

                store_proposals(session, a.project, row, "cli")
                summ = template_summary(row, project_aliases(session, a.project))
                session.commit()
                if a.json:
                    print(json.dumps(summ, indent=2, default=str))
                else:
                    rep = summ["report"]
                    print(f"template {summ['id']} for {a.object}: {rep['mapped']}/{rep['total']} fields mapped; mandatory unmapped: {rep['mandatory_missing'] or 'none'}; alias proposals: {len(summ['alias_proposals'])} (sdtf cockpit-template aliases --project {a.project})")
                return 0
            for ot, row in sorted(templates_for(session, a.project).items()):
                rep = template_summary(row)["report"]
                print(f"{ot}: {row.filename} ({row.sha256[:12]}) {rep['mapped']}/{rep['total']} mapped; mandatory unmapped: {rep['mandatory_missing'] or 'none'}")
        return 0
    if a.cmd == "cockpit-feedback":
        from sqlalchemy import select

        from .models import CockpitFeedback
        from .runtime.cockpit_attempts import attempt_out, attempts, burndown, mark_attempt
        from .runtime.cockpit_feedback import feedback_out, feedback_summary, import_feedback, sample_feedback

        with session_scope() as session:
            try:
                if a.fcmd == "rounds":
                    bd = burndown(session, a.run)
                    if a.json:
                        print(json.dumps(bd, indent=2, default=str))
                    else:
                        print(f"run {a.run}: {len(bd['rounds'])} round(s), {bd['remaining']} instance(s) still rejected, {bd['resolved_total']} resolved, converged={bd['converged']}")
                        for r in bd["rounds"]:
                            res = r["result"]
                            print(f"  round {r['sequence']} [{r['scope']}] {r['status']}: {r['instances']} instances, {r['rows']} rows" + (f"; rejected {res.get('rejected', 0)}, accepted {res.get('accepted', 0)}, not in log {res.get('not_in_log', 0)}, resolved {res.get('resolved', 0)}" if res else "") + (f"; uploaded by {r['uploaded_by']} ({r['upload_note']})" if r["uploaded_at"] else "") + (f"; migrated by {r['migrated_by']}" if r["migrated_at"] else ""))
                        for inst in bd["still_rejected"][:20]:
                            print(f"  still rejected: {inst['instance']} rounds {[h['round'] for h in inst['history']]}; last message: {inst['messages'][-1]['message'] if inst['messages'] else '-'}")
                    return 0
                if a.fcmd == "mark":
                    row = next((x for x in attempts(session, a.run) if x.sequence == a.round), None)
                    if row is None:
                        print(f"round {a.round} not found", file=sys.stderr)
                        return 2
                    out = attempt_out(mark_attempt(session, row, a.status, "cli", a.note))
                    session.commit()
                    print(f"round {out['sequence']} is now {out['status']}")
                    return 0
                if a.fcmd == "sample":
                    xml = sample_feedback(session, a.run)
                    if a.out:
                        with open(a.out, "w", encoding="utf-8") as fh:
                            fh.write(xml)
                        print(f"illustrative simulation log for run {a.run} written to {a.out}")
                    else:
                        print(xml)
                    return 0
                if a.fcmd == "import":
                    with open(a.file, encoding="utf-8") as fh:
                        content = fh.read()
                    summ = import_feedback(session, a.run, content, a.file.rsplit("/", 1)[-1], "cli", replace=not a.append, attempt_sequence=a.round)
                    session.commit()
                else:
                    summ = feedback_summary(session, a.run)
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 2
            if a.json:
                rows = session.execute(select(CockpitFeedback).where(CockpitFeedback.run_id == a.run)).scalars().all()
                print(json.dumps({"summary": summ, "messages": [feedback_out(f) for f in rows]}, indent=2, default=str))
            else:
                print(f"run {a.run} round {summ['round']}: {summ['messages']} messages ({summ['errors']} errors, {summ['warnings']} warnings), {summ['matched']} matched, {summ['unmatched']} unmatched; rejected instances {summ['rejected_instances']} of {summ['instances_in_log']} in the log (pass rate {summ['pass_rate']}); still rejected over all rounds: {summ['still_rejected']}, resolved {summ['resolved_total']}, converged={summ['converged']}")
                for ot, o in summ["objects"].items():
                    print(f"  {ot}: {o['instances']} instances, {o['with_messages']} in log, {o['rejected']} rejected, {o['errors']} E / {o['warnings']} W; categories {o['categories'] or '-'}")
                for reason, n in summ["unmatched_reasons"].items():
                    print(f"  unmatched x{n}: {reason}")
        return 0
    if a.cmd == "bench":
        from .benchmark import environment, report_markdown, run_benchmark, write_report

        results = []
        for sc in [int(x) for x in a.scales.split(",") if x.strip()]:
            with session_scope() as s_:
                r = run_benchmark(s_, scale=sc, seed=a.seed, workers=a.workers)
            results.append(r)
            print(f"scale {sc}: {r['landscape']['rows']} rows, run {r['run']['status']} in {r['steps']['run_s']} s, end to end {r['steps']['end_to_end_s']} s, reconciliation {r['reconciliation']['overall']} over {r['reconciliation']['checks']} checks")
        env = environment()
        if a.out:
            write_report(results, a.out, env)
            print(f"written {a.out}")
        if a.json:
            print(json.dumps({"environment": env, "results": results}, indent=2, default=str))
        elif not a.out:
            print(report_markdown(results, env))
        return 0
    if a.cmd == "discover":
        from .discovery.rfc_discovery import discover_over_rfc
        from .discovery.service import discover_system
        from .models import SapSystem

        with session_scope() as s_:
            system = s_.get(SapSystem, a.system)
            if system is None:
                print(f"system {a.system} not found", file=sys.stderr)
                return 2
            chosen = a.path or ("rfc" if system.connector == "RFC" else "record_store")
            snap = discover_over_rfc(s_, system, "cli", sample=a.sample) if chosen == "rfc" else discover_system(s_, system, "cli")
            summary, status = dict(snap.summary), snap.status
        if status == "FAILED":
            print(f"discovery failed: {summary.get('read', {}).get('error', '')}", file=sys.stderr)
            return 1
        if a.json:
            print(json.dumps(summary, indent=2, default=str))
        else:
            rd = summary.get("read", {})
            print(f"{summary['system']['sid']}/{summary['system']['client']} discovered through {rd.get('path')}{' (' + str(rd.get('transport')) + ')' if rd.get('transport') else ''}: {summary['tables']['count']} tables ({summary['tables']['custom']} custom), {summary['tables']['total_rows']} rows, org units {summary['org_units']}, {len(summary['business_objects'])} business object types, complexity {summary['complexity']['band']} ({summary['complexity']['score']})")
            if rd.get("unreadable"):
                print("not readable: " + ", ".join(f"{k} ({v})" for k, v in rd["unreadable"].items()))
            for n in rd.get("notes", []):
                print("note: " + n)
        return 0
    if a.cmd == "process-analysis":
        from .discovery import process_analysis as pa
        from .models import SapSystem

        with session_scope() as s_:
            system = s_.get(SapSystem, a.system)
            if system is None:
                print(f"system {a.system} not found", file=sys.stderr)
                return 2
            res = pa.analyse(s_, system, a.area, [c.strip() for c in a.bukrs.split(",")] if a.bukrs else None, a.top, a.retention_years)
        md = pa.report_markdown(res)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as fh:
                fh.write(md)
            print(f"written {a.out}")
        if a.json:
            print(json.dumps(res, indent=2, default=str))
        elif not a.out:
            print(md)
        return 0
    if a.cmd == "lookup-csv":
        from .rules.editor import lookup_yaml, parse_lookup_csv

        with open(a.file, encoding="utf-8-sig") as fh:
            rep = parse_lookup_csv(fh.read(), a.name, a.key_column, a.value_column)
        if a.json:
            print(json.dumps(rep, indent=2))
        elif rep["errors"]:
            for e in rep["errors"]:
                print("error: " + e, file=sys.stderr)
        else:
            print(lookup_yaml(a.name, rep["entries"]), end="")
            print(f"# {len(rep['entries'])} entries from {rep['rows']} rows ({rep['skipped']} skipped, {len(rep['duplicates'])} duplicates)", file=sys.stderr)
        return 0 if not rep["errors"] else 2
    if a.cmd == "metadata":
        from .runtime import metadata_check as mc

        if a.mdcmd == "expectations":
            services = [a.service] if a.service else mc.bound_services()
            exp = {s_: mc.expectations(s_) for s_ in services}
            if a.json:
                print(json.dumps(exp, indent=2))
            else:
                for s_, items in exp.items():
                    print(f"{s_}:")
                    for e in items:
                        print(f"  {e['entity_set']} ({e['table']}, {e['use']}): keys {', '.join(e['keys']) or '-'}; properties {', '.join(e['properties'])}")
            return 0
        if a.file:
            if not a.service:
                print("--service is required with --file", file=sys.stderr)
                return 2
            with open(a.file, encoding="utf-8") as fh:
                content = fh.read()
            try:
                res = mc.check_service(mc.parse_edmx(content), a.service)
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 2
        elif a.url:
            import os as _os

            from .runtime import target_api as tapi

            pw = _os.getenv(a.passwd_env, "")
            if a.user and not pw:
                print(f"set {a.passwd_env} in the environment (the password is never given on the command line)", file=sys.stderr)
                return 2
            dest = {"base_url": a.url, "user": a.user or "", "passwd": pw, "verify": not a.no_verify}
            if a.no_verify:
                print("warning: TLS certificate verification disabled", file=sys.stderr)
            try:
                res = mc.check_target(tapi.S4ApiHttpTransport(dest), [x.strip() for x in a.services.split(",")] if a.services else None)
            except tapi.ApiError as e:
                print(f"{e.code}: {e.message}", file=sys.stderr)
                return 3
            except Exception as e:  # noqa: BLE001 - connection errors from httpx
                print(f"could not reach {a.url}: {e}", file=sys.stderr)
                return 3
        elif a.system:
            from .models import SapSystem
            from .runtime import target_api as tapi

            with session_scope() as session:
                s_ = session.get(SapSystem, a.system)
                if s_ is None or s_.connector != "API":
                    print("system not found or not an API target", file=sys.stderr)
                    return 2
                try:
                    res = mc.check_target(tapi.make_target_transport(session, s_), [x.strip() for x in a.services.split(",")] if a.services else None)
                except tapi.ApiError as e:
                    print(f"{e.code}: {e.message}", file=sys.stderr)
                    return 3
        else:
            print("give --file with --service, --system, or --url", file=sys.stderr)
            return 2
        md = mc.report_markdown(res)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as fh:
                fh.write(md)
        if a.json:
            print(json.dumps(res, indent=2, default=str))
        else:
            print(md if not a.out else f"report written to {a.out}")
        return 0
    if a.cmd == "reconcile":
        from .runtime.pipeline import RunPrecondition, reconcile_again

        with session_scope() as session:
            try:
                summ = reconcile_again(session, a.run, "cli", mode=a.mode)
            except RunPrecondition as e:
                print(str(e), file=sys.stderr)
                return 2
            except Exception as e:  # noqa: BLE001
                print(f"reconciliation read failed: {e}", file=sys.stderr)
                return 3
            session.commit()
        if a.json:
            print(json.dumps(summ, indent=2, default=str))
        else:
            views = summ.get("views", {})
            print(f"run {a.run}: reconciliation {summ['overall']} ({summ['checks']} checks; " + ", ".join(f"{k} {v}" for k, v in summ["by_layer"].items()) + ")")
            for side in ("source", "target"):
                v = views.get(side) or {}
                print(f"  {side}: {v.get('origin', 'record_store')}" + (f" via {v['transport']}" if v.get("transport") else "") + (f" [{v['mode']}: {v.get('mode_decision', {}).get('reason', '')}]" if v.get("mode") else "") + (f", {v['rows']} rows read" if v.get("rows") is not None else "") + (f" ({v['rows_avoided']} line items not transferred)" if v.get("rows_avoided") else "") + (f", not readable: {', '.join(v['unreadable'])}" if v.get("unreadable") else ""))
            if summ.get("not_verified"):
                print(f"  not verified (no read path): {', '.join(summ['not_verified'])}")
        return 0
    if a.cmd == "cutover-rehearsal":
        from .cutover import rehearsal as reh
        from .models import CutoverRehearsal, ScopeManifest

        with session_scope() as session:
            try:
                if a.rcmd in ("create", "list"):
                    m = session.get(ScopeManifest, a.manifest)
                    if m is None:
                        print(f"manifest {a.manifest} not found", file=sys.stderr)
                        return 2
                    if a.rcmd == "create":
                        r = reh.create_rehearsal(session, m, a.name, a.kind, "cli")
                        session.commit()
                        rows = [r]
                    else:
                        rows = reh.rehearsals(session, m.id)
                    if a.json:
                        print(json.dumps([reh.rehearsal_out(x, full=(a.rcmd == "create")) for x in rows], indent=2, default=str))
                    else:
                        for x in rows:
                            s_ = x.summary or {}
                            print(f"rehearsal {x.sequence} [{x.kind}] {x.id}: {x.name} -- {x.status}{' ' + x.verdict if x.verdict else ''}; {s_.get('pass', 0)} PASS / {s_.get('fail', 0)} FAIL / {s_.get('not_applicable', 0)} N/A / {s_.get('pending', 0)} pending; blocking open: {', '.join(s_.get('blocking_open', [])) or 'none'}")
                    return 0
                r = session.get(CutoverRehearsal, a.id)
                if r is None:
                    print(f"rehearsal {a.id} not found", file=sys.stderr)
                    return 2
                if a.rcmd == "timeline":
                    from .cutover.execution import timeline

                    tl = timeline(session, r)
                    if a.json:
                        print(json.dumps(tl, indent=2, default=str))
                    else:
                        dt = tl["downtime"]
                        print(f"rehearsal {r.sequence} [{r.kind}] {r.name}: {tl['status']}; elapsed {tl['elapsed_minutes']} min of {tl['planned_total_minutes']} planned, projected {tl['projected_total_minutes']}; downtime {dt['elapsed_minutes']} / {dt['planned_minutes']} planned / {dt['projected_minutes']} projected min; late: {', '.join(tl['late']) or 'none'}; open incidents {tl['incidents']['open']} (blocking: {', '.join(tl['incidents']['blocking']) or 'none'}); assigned {tl['assignments']['assigned']}/{tl['assignments']['tasks']}")
                        for x in tl["tasks"]:
                            print(f"  {x['id']} {x['status']:<8} {x['name'][:52]:<52} est {x['est_minutes']:>6} actual {x['actual_minutes'] if x['actual_minutes'] is not None else '-':>6} {'late ' + str(x['late_minutes']) if x['late_minutes'] else '':<10} {x['assignee']}" + (f" [{x['source']}]" if x["observed"] else (f" (history: {x['history']['source']}, {x['history']['actual_minutes']} min)" if x.get("history") else "")))
                    return 0
                if a.rcmd == "incident":
                    from .cutover import execution as ex

                    if a.action == "raise":
                        inc = ex.raise_incident(session, r, a.task, a.severity, a.title, "cli", a.detail)
                        print(f"{inc['id']} raised on {inc['task']} ({inc['severity']}, {inc['status']}" + (f", escalated to {inc['escalations'][-1]['to']}" if inc["escalations"] else "") + ")")
                    elif a.action == "escalate":
                        inc = ex.escalate_incident(session, r, a.incident, "cli", a.note, a.to)
                        print(f"{inc['id']} escalated to level {inc['level']}: {inc['escalations'][-1]['to']}")
                    else:
                        inc = ex.resolve_incident(session, r, a.incident, "cli", a.note)
                        print(f"{inc['id']} resolved")
                    session.commit()
                    return 0
                if a.rcmd == "assign":
                    from .cutover.execution import assign_task

                    e_ = assign_task(session, r, a.task, "cli", a.assignee, a.backup, a.contact)
                    session.commit()
                    print(f"{a.task}: " + (f"{e_['assignee']}" + (f" (backup {e_['backup']})" if e_.get("backup") else "") if e_ else "assignment cleared"))
                    return 0
                if a.rcmd == "refresh":
                    res = reh.refresh_auto_items(session, r, "cli")
                    session.commit()
                    if a.json:
                        print(json.dumps({**reh.rehearsal_out(r), "changed": res["changed"]}, indent=2, default=str))
                    else:
                        print(f"refreshed: {len(res['changed'])} item(s) changed; blocking open: {', '.join(res['summary']['blocking_open']) or 'none'}")
                    return 0
                if a.rcmd == "start":
                    reh.start_rehearsal(session, r, "cli", a.note)
                elif a.rcmd == "mark":
                    it = reh.mark_item(session, r, a.item, a.status, "cli", a.note)
                    print(f"{it['id']} {it['title']}: {it['status']}")
                elif a.rcmd == "task":
                    t = reh.time_task(session, r, a.task, a.action, "cli", a.note)
                    print(f"{a.task} {a.action}ed" + (f": {t['actual_minutes']} min" if t.get("actual_minutes") is not None else ""))
                elif a.rcmd == "lesson":
                    reh.add_lesson(session, r, a.text, "cli", a.task)
                elif a.rcmd == "complete":
                    reh.complete_rehearsal(session, r, a.verdict, "cli", a.note)
                elif a.rcmd == "abort":
                    reh.abort_rehearsal(session, r, "cli", a.note)
                elif a.rcmd == "report":
                    md = reh.rehearsal_markdown(r)
                    if a.out:
                        with open(a.out, "w", encoding="utf-8") as fh:
                            fh.write(md)
                        print(f"report written to {a.out}")
                    else:
                        print(md)
                    return 0
                session.commit()
                if a.rcmd == "show":
                    if a.json:
                        print(json.dumps(reh.rehearsal_out(r), indent=2, default=str))
                    else:
                        s_ = r.summary or {}
                        print(f"rehearsal {r.sequence} [{r.kind}] {r.name}: {r.status}{' ' + r.verdict if r.verdict else ''}; blocking open: {', '.join(s_.get('blocking_open', [])) or 'none'}")
                        for it in r.items:
                            print(f"  {it['id']} {it['status']:<14} {'B' if it['blocking'] else ' '} {it['kind']:<6} {it['title']}" + (f" -- {it['detail'] or it['note']}" if it.get("detail") or it.get("note") else ""))
                        for k, t in sorted((r.timings or {}).items()):
                            print(f"  task {k}: {t.get('actual_minutes') if t.get('actual_minutes') is not None else 'running' if t.get('started_at') else '-'} min")
                        for lesson in r.lessons or []:
                            print(f"  lesson: {lesson['text']} ({lesson['by']})")
                else:
                    print(f"rehearsal {r.sequence} is now {r.status}{' ' + r.verdict if r.verdict else ''}")
            except (ValueError, LookupError) as e:
                print(str(e), file=sys.stderr)
                return 2
        return 0
    if a.cmd == "cockpit-export":
        from .runtime.cockpit_export import export_cockpit_files

        formats = ("csv", "xml") if a.format == "both" else (a.format,)
        with session_scope() as session:
            try:
                out = export_cockpit_files(session, a.run, out_dir=a.out, actor="cli", formats=formats, scope=a.scope)
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 2
        if a.json:
            print(json.dumps(out, indent=2, default=str))
        else:
            print(f"cockpit package: {out['zip']} ({out['files']} files, {out['rows']} rows, {len(out['objects'])} objects)")
            for ot, o in out["objects"].items():
                print(f"  {ot}: {o['rows']} rows in {', '.join(o['tables'])} -> {o['migration_object']}")
        return 0
    if a.cmd == "worker":
        from . import observability as obs
        from .db import get_engine, get_session_factory
        from .runtime.worker import Worker

        obs.setup(service_name=__import__("os").getenv("OTEL_SERVICE_NAME", "sdtf-worker"))
        obs.instrument_engine(get_engine())
        w = Worker(get_session_factory(), a.worker_id, a.lease_seconds)
        n = w.run(until_idle=a.until_idle, max_jobs=a.max_jobs)
        print(f"worker {w.worker_id} processed {n} job(s)")
        return 0
    if a.cmd == "serve":
        import uvicorn

        uvicorn.run("sdtf.main:app", host=a.host, port=a.port, reload=False)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
