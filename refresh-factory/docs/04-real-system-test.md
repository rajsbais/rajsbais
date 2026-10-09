# First contact with a real SAP system (read-only)

**Status: nothing in this repository has ever run against a real SAP system.** This page is how to do the first run safely, one system at a
time, on the machine where the systems live. The platform can only *read* from a real system (there is no write path and no loader yet), so
this tests extraction, mapping and planning, not refreshing a real target.

## Ground rules
- Use a **sandbox, trial, or a copy**, never production. The tool is read-only and bounded, but a first run is still a first run.
- Use a **dedicated read-only SAP user**. Put its password in an environment variable; the tools only take the variable's *name*.
- Start with the smoke test below. It writes a report with counts, field names, widths, timings and error classes: **no row values**, so it can be shared.

## 1. Get the platform onto that machine
```
git clone <repo> && cd <repo> && git checkout claude/lucid-euler-7xqkbg
cd refresh-factory/backend && pip install -e '.[test]'
python -m pytest -q            # should end in "passed" before you point it at anything
```

## 2. ECC, over RFC
Needs the **SAP NetWeaver RFC SDK** (from the SAP Support Portal; it cannot be redistributed) and `pip install pyrfc`. Without them the tool says so and stops.

Reach the application server from the machine running the tool (host/IP, instance number: gateway port is 33*NN*, dispatcher 32*NN*; for a VM make sure the port is forwarded).
The user needs, typically (verify with `SU53` / an authorization trace; names vary by release):
`S_RFC` for the function groups of `RFC_READ_TABLE`, `DDIF_FIELDINFO_GET`, `RFC_SYSTEM_INFO`, `RFC_PING`; and `S_TABU_DIS` (read) for the table authorization groups of the tables in scope.

```
export SAP_PW='...'
python -m rfactory.sap.connectors.smoke rfc --ashost <host> --sysnr 00 --client 100 \
  --user RFREAD --password-env SAP_PW --change-documents --out reports --yes
```
Read the **Verdict** first. `BLOCKER` = could not connect or unexpected failure; `ATTENTION` = works but differs (tables the user may not read, tables missing in this release, fields that differ from the platform's model, keys arriving without leading zeros); `INFO` = counts.

## 3. S/4HANA
- **RFC:** the same command against the S/4HANA host.
- **OData** (for cloud/PCE, or when RFC is not allowed): activate the services in `/IWFND/MAINT_SERVICE` (purchase order, billing document, product, company code, plant, sales order) and give the user the matching service authorizations.
  ```
  python -m rfactory.sap.connectors.smoke odata --base-url https://<host>:<port> --user RFREAD --password-env SAP_PW --out reports --yes
  ```
  The entity and property names in `sap/connectors/odata.py` are written from memory: the **drift list in the report is exactly the list of names to correct**. Tables with no mapping show as `unmapped`; that is expected.

## 4. Then, and only then, connect it to the platform
Start the API with the password available to *its* process (`export SAP_PW=...`), then register the system (read-only source):
```
curl -X POST localhost:8000/api/systems/connect -H 'X-Demo-User: root.admin' -H 'Content-Type: application/json' -d '{
  "system": {"sid": "ECC", "client": "100", "role": "SBX", "owner": "me"},
  "profile": {"name": "ecc-sandbox", "kind": "rfc", "ashost": "<host>", "sysnr": "00", "client": "100", "user": "RFREAD",
              "password_ref": "env:SAP_PW", "max_scan_rows": 20000, "calls_per_minute": 120}}'
```
Use it as the **source** of a small selective refresh into a *simulated* target (e.g. materials of one plant) and look at the plan, the masking advice and the preview. Real targets cannot be written yet.

## What to send back
The `.md` report from each run, and the exact error text of anything that failed. That is enough to correct the mapping, authorizations guidance and quirks, and is the first evidence this code has ever had from a real system.

## Not covered by this test
Writing into SAP, volumes and throughput at production scale, the change-document reader on large systems, OData V4, token or SSO authentication for RFC, and anything touching production.
