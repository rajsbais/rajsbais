# 09 — Repository and deployment architecture

```
.
├── backend/            FastAPI + SQLAlchemy + domain packages (sdtf/*), Alembic migrations, pytest suite
├── frontend/           Vite + React + TypeScript, 18 screens
├── deploy/             Dockerfiles, nginx, docker-compose, Kubernetes (kustomize)
├── docs/               architecture, specs, ADRs, operations
├── sap-abap/           add-on interface contract (planned)
└── .github/workflows/  CI: lint, tests, vertical slice smoke, migration check, frontend build, container build
```

## Run locally
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e "backend[dev]"
cd backend && python -m sdtf.cli demo            # vertical slice on SQLite, prints the execution report
python -m sdtf.cli serve                          # API on :8000 (OpenAPI at /docs)
cd ../frontend && npm install && npm run dev      # UI on :5173, proxies /api to :8000
```
Dev users: admin / architect / approver / operator / auditor / viewer (password = username).

## Compose (PostgreSQL + API + UI)
`docker compose -f deploy/docker-compose.yml up --build` → UI on :8080, API on :8000. Alembic migrations run at
container start.

## Kubernetes
`kubectl apply -k deploy/k8s` — namespace, ConfigMap, Secret (replace values via External Secrets/Vault), backend
Deployment (2 replicas, non-root, probes, evidence PVC), frontend Deployment, Ingress with TLS. Horizontal worker
scaling: `deploy/k8s/worker.yaml` runs `sdtf worker` pods (HPA 2–32) that claim extraction jobs of DISTRIBUTED runs;
INLINE runs still use threads inside the API pod (`SDTF_EXTRACTION_WORKERS`). API and workers share the RWX volume that holds
evidence; with `SDTF_STAGING_DIR=s3://…` or `gs://…` staging needs no shared filesystem at all.

## Configuration
| Variable | Purpose |
|---|---|
| SDTF_DATABASE_URL | `sqlite:///./data/sdtf.db` (default) or `postgresql+psycopg://…` |
| SDTF_AUTH_SECRET, SDTF_TOKEN_TTL | token signing |
| SDTF_DEV_USERS | `1` seeds dev users (set `0` in production) |
| SDTF_EVIDENCE_DIR | evidence package location (object storage mount in production) |
| SDTF_EXTRACTION_WORKERS | parallel extraction workers |
| SDTF_CORS_ORIGINS | allowed UI origins |
| SDTF_STAGING_BACKEND, SDTF_STAGING_DIR | `relational` (default) or `columnar` Parquet staging at a local path, `s3://bucket/prefix`, `gs://bucket/prefix` or `memory://` |
| SDTF_STAGING_FS_OPTIONS | JSON fsspec options for the staging filesystem, e.g. `{"endpoint_url": "http://minio:9000"}` |
| SDTF_JOB_LEASE_SECONDS, SDTF_WORKER_POLL_SECONDS | distributed worker lease and poll interval |
| OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME, SDTF_OTEL_ENABLED, SDTF_OTEL_CONSOLE, SDTF_LOG_FORMAT | telemetry export and log format (ADR-0011) |
| SDTF_GRAPH_BACKEND, SDTF_NEO4J_URI, SDTF_NEO4J_USER, SDTF_NEO4J_PASSWORD, SDTF_NEO4J_DATABASE | dependency-graph store: `relational` (default) or `neo4j` (ADR-0004) |

## Observability
OpenTelemetry traces and metrics (ADR-0011) exported over OTLP/HTTP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set
(`SDTF_OTEL_CONSOLE=1` for stdout in development); FastAPI and SQLAlchemy auto-instrumented; JSON logs with
`trace_id`/`span_id` via `SDTF_LOG_FORMAT=json`. Stage metrics and durations are also persisted per run
(`run_stages.metrics`) and shown in the UI; `/healthz` reports telemetry status; `/api/v1/platform/telemetry` too.
