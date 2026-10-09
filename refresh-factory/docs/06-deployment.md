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
No TLS, no reverse proxy, no secrets manager, no network policy, no backup of the `rf-data` volume (back it up together with its keys, separately), no horizontal scaling (state is one SQLite file with a single writer), no rate limiting beyond the cap on audit growth for failed logins, no WAF. These are the operator's.
