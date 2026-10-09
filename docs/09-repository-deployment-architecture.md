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
INLINE runs still use threads inside the API pod (`SDTF_EXTRACTION_WORKERS`). API and workers share the RWX volume that
holds evidence and columnar staging.

## Configuration
| Variable | Purpose |
|---|---|
| SDTF_DATABASE_URL | `sqlite:///./data/sdtf.db` (default) or `postgresql+psycopg://…` |
| SDTF_AUTH_SECRET, SDTF_TOKEN_TTL | token signing |
| SDTF_DEV_USERS | `1` seeds dev users (set `0` in production) |
| SDTF_EVIDENCE_DIR | evidence package location (object storage mount in production) |
| SDTF_EXTRACTION_WORKERS | parallel extraction workers |
| SDTF_CORS_ORIGINS | allowed UI origins |
| SDTF_STAGING_BACKEND, SDTF_STAGING_DIR | `relational` (default) or `columnar` Parquet staging under the given object-storage mount |
| SDTF_JOB_LEASE_SECONDS, SDTF_WORKER_POLL_SECONDS | distributed worker lease and poll interval |

## Observability (partial)
Structured stage metrics and durations are persisted per run (`run_stages.metrics`) and exposed in the UI;
`/healthz` for probes. Planned: OpenTelemetry traces/metrics export, structured JSON logging, per-partition progress events.
