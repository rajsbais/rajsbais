# Keystone — SAP Intelligent Refresh Factory (MVP)

Selective, business-consistent SAP non-production refresh with masking, conflict control and reconciliation, for **SAP ECC 6.0** and **SAP S/4HANA**.

> **Honest status:** this is a working vertical slice running against a **simulated** SAP adapter with synthetic data (ECC EP1/100→EQ1/200 and S/4HANA S4P/100→S4Q/200).
> No real SAP system is read or written. See [`docs/01-capability-matrix.md`](docs/01-capability-matrix.md) for what is real, simulated, catalogued or planned.

## Run
```bash
cd backend && pip install -e '.[test]' && python -m pytest          # 148 tests
cd ../frontend && npm install && npm run build                       # UI served by the API from frontend/dist
cd ../backend && uvicorn rfactory.api.main:app --port 8000           # http://localhost:8000  (API docs at /docs)
# UI dev server: cd frontend && npm run dev   (proxies /api to :8000)
```
Sign in with the sidebar user switcher (demo header auth). Walkthrough: *Control tower → Load synthetic landscape → Selective designer* (create project, scope company 1000 / 90 days,
build plan, analyze conflicts, apply SKIP policy, apply masking advice, submit; switch to **Carol (Change approver)** to approve; switch back to execute) → *Reconciliation*.

## What the slice demonstrates
Register systems · discover company codes/plants/objects · scope by company code and date · expand master + document-flow dependencies · volume preview ·
conflict detection (identical/modified target objects, tester-owned documents, missing customizing, number ranges) · masking with discovery of custom PII fields ·
approval with separation of duties · checkpointed load, resume, rollback · 34+ technical/business/security checks with a release gate · report + evidence ZIP + hash-chained audit.

**Delta refresh:** *Delta refresh* screen → create the weekend QA sync, add the recommended masking rule (Dave), submit (Alice), approve (Carol), run; then *Simulate source activity* and preview/run again to see only the difference move.

**Test data catalog:** *Test catalog* screen → create the default policy (Alice), submit, approve (Carol), request materials as Alice (subset), then as Tina request an O2C chain (subset or synthetic), record usage, release; curators can scan the target, make datasets golden and restore them.

**Lean client builder:** *Lean client* screen → create a template from a purpose preset (Alice), submit, approve (Carol), estimate lean vs full client copy, build client 320 into the QA system, then lock/unlock or decommission it.

Docs: [capability matrix](docs/01-capability-matrix.md) · [architecture](docs/02-architecture.md) · [backlog](docs/03-backlog.md) · [OpenAPI](docs/openapi.json) · [DB schema (design)](db/schema.sql).
