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
