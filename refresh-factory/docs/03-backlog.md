# Engineering backlog (ordered)

1. **Persistence**: PostgreSQL repositories behind `RefreshService` (schema in `db/schema.sql`), durable runs, object-store staging.
2. **Real SAP connectivity** (also unlocks real client creation/copy for the lean client builder: SCC4/SCCL/SCC9 integration, user and authorization handling): RFC/OData adapter implementing `SourceAdapter`/`TargetAdapter` with metadata from DD02L/DD03L; read-only role on PRD; contract tests against a sandbox ECC and S/4HANA.
3. **ABAP agent** (extraction + non-prod loader) and number-range handling with SNRO APIs.
4. **SSO/ABAC/tenancy/KMS**, WORM audit sink.
5. **Durable orchestration** (orchestrator exists, simulated): replace the in-memory queue with Temporal/Argo, parallel workers with throttling, a clock-driven daemon, real notification channels, leases enforced inside every module.
6. **Delta refresh hardening** (engine exists, simulated): real change-document readers per object class, durable scheduler daemon, parallel packages, mechanism profiles from metadata.
7. **More objects**: PP/QM/PM/PS/WM-EWM, S/4 MATDOC, HR/payroll masking, key remapping (REMAP).
8. **Full refresh adapters** (SWPM, HANA, storage/VM snapshot) and **real adapters for the post-copy tasks** (the factory and its safety logic exist against simulated state).
9. **TDM hardening** (catalog exists, simulated): Cloud ALM / Jira / Xray connectors, outbound webhooks, templates for PP, FI-AA, MM-IM and full intercompany, persistent catalog.
10. **Benchmarks**: controlled throughput tests replacing the placeholder duration model.
11. **UI**: remaining screens, e2e tests, accessibility audit.
