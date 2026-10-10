# Deployment and hardening

**Status:** the container files (`Dockerfile`, `docker-compose.yml`, `.env.example`) have never been built or run by the authors (no container runtime was available). The HTTP hardening and the production switch in `rfactory/api/hardening.py` are tested.

## Production switch
`RFACTORY_ENV=production` makes the API **refuse to start** unless: authentication is `oidc`; state is durable (`RFACTORY_DATA_DIR`); `RFACTORY_STATE_KEY` and `RFACTORY_AUDIT_KEY` come from the environment (not from key files next to the data); the fake-endpoint test hook is off. It also switches `/api/demo/*` off (404). The error lists every problem.

## Always on
Security headers on every answer: a Content-Security-Policy limited to this origin (plus the identity provider's origin for the browser login's token request), no framing, no content sniffing, no referrer, no camera/microphone/location, `Cache-Control: no-store` for API answers, and a 5 MB request-body limit (declared `Content-Length` only). `RFACTORY_HSTS=1` adds HSTS and is only correct behind HTTPS.

## Run it
```
cp .env.example .env     # fill in the identity provider and generate the two keys
docker compose up --build
```
The port is published on `127.0.0.1:8088` only: **put a TLS-terminating reverse proxy in front of it** (and set `FORWARDED_ALLOW_IPS` for uvicorn's `--proxy-headers` to that proxy). The container runs as an unprivileged user with a read-only root filesystem and no extra capabilities; state lives in the `rf-data` volume, encrypted at rest.

RFC connections need the SAP NetWeaver RFC SDK and `pyrfc` inside the image; neither can be redistributed, so they are not in the Dockerfile. OData connections need nothing extra. Secrets for connection profiles are environment variables named in the profile (`password_ref: env:NAME`), passed through `.env`.

## What this does not do
No TLS, no reverse proxy, no secrets manager, no network policy, no backup of the `rf-data` volume (back it up together with its keys, separately), no automatic scaling or failover (several instances are possible with PostgreSQL, see below; writes are serialised on purpose), no rate limiting beyond the cap on audit growth for failed logins, no WAF. These are the operator's.


## Several instances on PostgreSQL

By default the platform keeps its state in one encrypted SQLite file and runs as ONE process. To run several instances behind a load balancer, point them all at one PostgreSQL database:

```bash
RFACTORY_DATABASE_URL=postgresql://rf:SECRET@db:5432/rf   # needs the driver: pip install 'psycopg[binary]'  (the Dockerfile installs it)
RFACTORY_STATE_KEY=...    # the SAME value on every instance (a Fernet key)
RFACTORY_AUDIT_KEY=...    # the SAME value on every instance
```

`docker-compose.postgres.yml` is an example with two instances (never run). Without both keys in the environment an instance refuses to start (a key file would differ between instances).

How it works, and what it costs:
- **One write lock for the whole deployment.** A mutating request takes a PostgreSQL advisory lock, first catches up with what the other instances saved, does its work, saves, and releases the lock. Writes are serialised across instances on purpose: this is an admin tool, not a high-write system, and it makes lost updates impossible. A busy lock answers `503` after `RFACTORY_WRITE_LOCK_TIMEOUT` seconds (default 60) without changing anything. If an instance dies mid-request PostgreSQL releases its lock at once.
- **Reads** are never blocked; each one costs one small query that checks a version counter and, when another instance has saved, pulls the changed objects.
- **State** is the same encrypted objects as in SQLite: PostgreSQL holds ciphertext and the wrapped data key, never the key-encryption key. Rotating the data key (`POST /api/persistence/rotate-data-key` or `python -m rfactory.persistence.rotate --database-url-env VAR --rotate-data-key`) is noticed by the other instances at their next request.
- **The audit log** lives in an append-only table (a trigger refuses UPDATE, DELETE and TRUNCATE) and every instance extends the same signed hash chain. The trigger does not stop the database owner; the signed chain and an escrowed head (`/api/audit/head`) are what detect that.

Not provided: a connection pool (each instance uses two connections), automatic reconnection in the middle of a request (the request fails and the client retries), backups, replication and failover (the database operator's), per-tenant schemas or row-level security, and the throughput of anything but a handful of writes per second. Each instance holds the whole state in memory, so memory grows with the number of projects and runs.
