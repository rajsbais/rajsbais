# Engineering backlog (ordered)

1. **Persistence** (SQLite state store exists, see M16): PostgreSQL repositories behind `RefreshService` (schema in `db/schema.sql`), migrations, KMS-held keys, backup/restore, multi-writer concurrency, object-store staging.
2. **Real SAP connectivity** (also unlocks real client creation/copy for the lean client builder: SCC4/SCCL/SCC9 integration, user and authorization handling): RFC/OData adapter implementing `SourceAdapter`/`TargetAdapter` with metadata from DD02L/DD03L; read-only role on PRD; contract tests against a sandbox ECC and S/4HANA.
3. **ABAP agent** (extraction + non-prod loader) and number-range handling with SNRO APIs.
4. **Security hardening** (OIDC bearer auth, system/company ABAC, envelope encryption and signed audit exist, see M15): Authorization Code + PKCE login, token revocation, KMS/HSM providers, plant/org/row-level ABAC, tenancy, WORM/object-lock audit sink and SIEM export, real-IdP tests.
5. **Durable orchestration** (orchestrator exists, simulated): replace the in-memory queue with Temporal/Argo, parallel workers with throttling, a clock-driven daemon, real notification channels, leases enforced inside every module.
6. **Delta refresh hardening** (engine exists, simulated): real change-document readers per object class, durable scheduler daemon, parallel packages, mechanism profiles from metadata.
7. **More objects** (PP orders/BOMs and MM-IM goods movements exist, see M6): QM/PM/PS/WM-EWM, routings/operations and confirmations, batches/stock/valuation, HR/payroll masking, key remapping (REMAP).
8. **Full refresh adapters** (SWPM, HANA, storage/VM snapshot) and **real adapters for the post-copy tasks** (the factory and its safety logic exist against simulated state).
9. **TDM hardening** (catalog exists, simulated): Cloud ALM / Jira / Xray connectors, outbound webhooks, templates for PP, FI-AA, MM-IM and full intercompany, persistent catalog.
10. **Benchmarks**: controlled throughput tests replacing the placeholder duration model.
11. **UI**: remaining screens, e2e tests, accessibility audit.
