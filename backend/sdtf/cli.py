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
    w = sub.add_parser("worker", help="run a distributed extraction worker (claims partition jobs)")
    w.add_argument("--worker-id", default=None)
    w.add_argument("--until-idle", action="store_true", help="exit when no job is available")
    w.add_argument("--max-jobs", type=int, default=None)
    w.add_argument("--lease-seconds", type=int, default=None)
    s = sub.add_parser("serve", help="run the API server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
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
            out = run_vertical_slice(session, scale=a.scale, seed=a.seed)
            run = out["run"]
            if a.json:
                print(json.dumps({"project_id": out["project"].id, "manifest_id": out["manifest"].id, "run_id": run.id, "status": run.status, "reconciliation": run.report["reconciliation"]}, indent=2, default=str))
            else:
                print(run.report["markdown"])
                print(f"project_id={out['project'].id} manifest_id={out['manifest'].id} run_id={run.id}")
        return 0
    if a.cmd == "worker":
        from .db import get_session_factory
        from .runtime.worker import Worker

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
