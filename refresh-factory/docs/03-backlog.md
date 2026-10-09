# Engineering backlog (ordered)

1. **Persistence**: PostgreSQL repositories behind `RefreshService` (schema in `db/schema.sql`), durable runs, object-store staging.
2. **Real SAP connectivity**: RFC/OData adapter implementing `SourceAdapter`/`TargetAdapter` with metadata from DD02L/DD03L; read-only role on PRD; contract tests against a sandbox ECC and S/4HANA.
3. **ABAP agent** (extraction + non-prod loader) and number-range handling with SNRO APIs.
4. **SSO/ABAC/tenancy/KMS**, WORM audit sink.
5. **Durable orchestration** (Temporal/Argo): queues, parallel extraction, throttling, maintenance windows, notifications.
6. **Delta refresh hardening** (engine exists, simulated): real change-document readers per object class, durable scheduler daemon, parallel packages, mechanism profiles from metadata.
7. **More objects**: PP/QM/PM/PS/WM-EWM, S/4 MATDOC, HR/payroll masking, key remapping (REMAP).
8. **Full refresh adapters** (SWPM, HANA, storage/VM snapshot) and executable post-copy tasks.
9. **TDM factory**: catalog, O2C/P2P templates, reservations, CI/CD and Cloud ALM hooks.
10. **Benchmarks**: controlled throughput tests replacing the placeholder duration model.
11. **UI**: remaining screens, e2e tests, accessibility audit.
