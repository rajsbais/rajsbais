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
    c.add_argument("--json", action="store_true")
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
    if a.cmd == "cockpit-export":
        from .runtime.cockpit_export import export_cockpit_files

        formats = ("csv", "xml") if a.format == "both" else (a.format,)
        with session_scope() as session:
            try:
                out = export_cockpit_files(session, a.run, out_dir=a.out, actor="cli", formats=formats)
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
