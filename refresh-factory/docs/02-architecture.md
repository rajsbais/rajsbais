# Architecture and design

## 1. Components (what exists today)
```
React/TS control tower (frontend/)  ──REST──▶  FastAPI (backend/rfactory/api)
                                                  │ RefreshService (workflow, governance gates)
        ┌───────────────┬───────────────┬─────────┴───────┬───────────────┬──────────────┐
  dependency/        selective/       masking/          reconcile/      security/      fullrefresh/ agents/
  registry+planner   manifest,        discovery +       tech/business/  RBAC, SoD,     phase model,  advisors
  (graph, cycles)    conflicts,       engine + vault    security checks hash-chained   post-copy     (rule based)
                     executor                           + release gate  audit          catalog
                                       ▼
                        sap/ adapter contracts  ──▶ SimulatedSap (ECC + S/4HANA)    [real RFC / OData / ABAP agent: NOT BUILT]
```
Selective copy, client copy and full system copy are separate workflows. Only selective copy has an executor; the others are modelled but not executable.

## 2. Canonical metadata and object model
`sap/ddic.py` holds a reduced DDIC (27 tables). Product families differ in object shape: **ECC** customer = KNA1/KNB1/KNVV/KNBK/ADRC, FI doc = BKPF/BSEG;
**S/4HANA** adds the Business Partner (BUT000, CVI-linked to KNA1/LFA1) and the universal journal ACDOCA. `Registry(family)` composes the family-specific model
(`dependency/registry.py`). Customizing (T001, T001W) is a *prerequisite verified in the target*, never copied.

## 3. Dependency graph
Business relationships (e.g. billing→delivery→order, FI document←billing via AWTYP/AWKEY) are declared as `Relationship` records, `REQUIRES` (copied) or `CONFIG` (verified).
Planner: root selection by filters → recursive closure over REQUIRES → optional downstream walk (reverse relationships) → customizing prerequisites → Tarjan cycle detection (type and instance level) →
Kahn topological load order. Blocking issues: dangling reference, cycle, unsupported filter, empty scope. Source validation reports orphans and dangling references. Custom Z objects register via `register_object_type/relationship`.

## 4. Selective copy execution
Versioned manifest (`content_hash` over everything that changes execution) → plan → conflict report → approval bound to the hash → executor:
PREFLIGHT (target writable, hash match) → EXTRACT_MASK_STAGE (in-flight masking; only masked rows staged, SHA-256 of staging files) → LOAD (checkpoint per business object, bounded retry on `TransientError`,
idempotent upserts, prior images recorded) → NUMBER_RANGES (raise target level to max loaded) → reconciliation → release gate. Resume continues from the checkpoint; rollback replays the undo log.

## 5. Conflict management
DUPLICATE_IDENTICAL / DUPLICATE_DIFFERENT / CONFIG_MISSING / NUMBER_RANGE / CHAIN_INCONSISTENT. Actions SKIP, FAIL, UPDATE (masters only), REPLACE (per-object approved exception), QUARANTINE; REMAP is rejected as unimplemented.
A skipped or quarantined *document* quarantines every dependent (no document attaches to a different target object). Missing org-level customizing for a master drops only those rows.

## 6. Masking
Catalog + value-pattern discovery; substitution is character-class/length preserving with valid IBAN check digits (not NIST FF1). Modes: PSEUDONYMIZE (keyed, stable, re-identifiable with key), ANONYMIZE (per-run key destroyed),
TOKENIZE (Fernet vault, re-identification needs `privacy_officer`). Keys cannot be masked. Submission requires every discovered sensitive field to be covered; release requires residual-PII and pattern scans to pass.

## 6b. Delta refresh (`backend/rfactory/delta/engine.py`)
A *scenario* is a standing, approved refresh: several **rolling scopes** (e.g. customers, vendors, materials, sales orders, purchase orders; `rolling_days` is resolved from the source's reference date on every run).
Approval is bound to a configuration hash (scope, policy, masking rules, targets) with separation of duties; schedule edits do not need re-approval, any other change does. Delta runs require stable masking (PSEUDONYMIZE/TOKENIZE): per-run ANONYMIZE keys are rejected because names would differ between objects loaded in different runs.

Each run: capture `to_seq` *before* reading the source → read change documents since the watermark → map changed rows to business objects → select candidates by mechanism → compare content hashes with what was last loaded → build the delta plan (new + changed + deferred) → conflict analysis → checkpointed execution → reconciliation → commit.

| Mechanism | Used for | Finds |
|---|---|---|
| `change_documents` | masters, orders, deliveries, POs | any change from the change log |
| `created_only` | billing, FI documents | only *new* documents from the log; modifications are left to the full sweep and reported as ignored |
| `full_compare` | custom/unknown objects | every tracked object is hash-compared each run |

A **full sweep** (first run, every N runs, on demand, or forced by a change-log gap) hash-compares every object. Objects a scenario loaded are *owned*: unchanged-in-target ones are replaced, **target drift** (edited in the target since) follows the `TARGET_DRIFT` policy (FAIL default, SKIP, REPLACE). Foreign objects use the normal duplicate/number-range analysis, so colliding keys are never overwritten. Skipped/quarantined objects are *deferred* and re-evaluated each run; skipped changes stay *stale* until applied.
The watermark advances only when the run completed **and** the release gate passed. A held run needs rollback or an audited acknowledgement; a failed run can be resumed or rolled back; scheduled runs skip scenarios needing attention. Source deletions and objects leaving the window are reported and never deleted from the target. `POST /api/delta/tick` is called by an external scheduler (service account `svc.scheduler`, which can only trigger approved scenarios); runs outside the window are recorded as missed.

## 6c. Test data management (`backend/rfactory/tdm/`)
**Templates** describe business scenarios, not tables (`templates.py`); candidate finders run on any read adapter, so the same logic subsets from the source, catalogs what already exists in the target, and validates synthetic output.
Scenarios the data model cannot express (make-to-stock/order, asset accounting, inventory/WM, goods/invoice receipt, full intercompany) are listed as *planned* with the reason and are rejected if requested.

**Governance.** A *policy* per target (allowed templates, objects per request, reservations per user, max reservation days, retention, extra masking rules, subset/synthetic switches) is approved once by an approver who is not its author; masking rules found by discovery over the whole source are added automatically and shown to the approver. Requests inside the policy run immediately under that standing approval; over the object quota they wait for an approver (never the requester, never an agent); anything else is rejected with reasons. Testers and CI service accounts can request, reserve, release and record usage; curation (golden, restore, retire, scan) needs `tdm:curate`; purge needs a human approver.

**Provisioning** reuses the platform: `subset` plans each root with the dependency planner (organisation-level rows restricted to the requested company code, roots the target cannot host are skipped), `synthetic` generates a consistent chain (new partners in reserved number spaces, document numbers from the target's own ranges, only materials really maintained in the target's plants, balanced accounting, S/4 Business Partner and ACDOCA rows). Both pass the same masking-coverage check, conflict analysis, checkpointed executor and reconciliation release gate; a failed gate rolls the target back and registers nothing.
**Catalog** entries are reservable datasets (root object + dependency closure) with business handles (document numbers) for testers and pipelines, a row-hash baseline for drift verification, and provenance `subset | synthetic | discovered`. `scan` registers complete scenarios already present in the target and skips objects owned by testers.
**Lifecycle**: exclusive reservations with TTL; consume/usage history with external test-case references; a *golden* dataset snapshots its verified rows and `restore` returns it to that snapshot (REPLACE through the executor, undo-logged, verified); sweep releases expired reservations and expires datasets past retention; purge deletes only rows the platform created, never objects still referenced by another live dataset, and refuses modified rows unless forced.

## 7. Full refresh and post-copy (design level)
13-phase runbook, pair guard (never a PRD target, no heterogeneous DB, no ECC↔S/4), 11 post-copy task definitions with prerequisites, pre/post-check, rollback, evidence, approval.
Execution needs SWPM/HANA/snapshot adapters — not built.

## 8. ABAP agent architecture (design, not built)
Original add-on in a customer namespace (`/KSTN/`): released-API-first extraction (RFC-enabled function modules + OData), package-based object readers mirroring `Relationship` records, change-document reader for delta,
authorization object `/KSTN/EXTR` (display only, per object type), `/KSTN/LOAD` for non-production only (target-side system role check in the add-on itself, independent of the control plane), throttling by work-process quota,
checkpointed packages with checksums, no direct DB writes. Delivered per release as separate transports for ECC 6.0 EHP8 and S/4HANA; PCE restricted to released APIs.

## 9. Security architecture
Implemented: RBAC with separation of duties (creator/editor/submitter ≠ approver), AI-agent principal stripped of approve/execute/re-identify, read-only source view, non-production write guard, hash-chained audit.
Designed, **not** implemented: OIDC/SAML SSO, ABAC, tenant isolation, KMS/HSM keys, encryption at rest, WORM audit sink, export-control classification, retention jobs. Authentication is a demo header.

## 10. UI navigation
Overview (Control tower, Landscape, Readiness) · Refresh (Selective designer, Dependencies, Conflicts, Masking, Execution, Reconciliation) · Basis (Full refresh, Post-copy) · Data (Test catalog [planned], Audit, Copilot).

## 11. Repository structure
`backend/rfactory/{sap,dependency,selective,masking,reconcile,security,fullrefresh,agents,api}` · `backend/tests` · `frontend/src` · `db/schema.sql` · `docs/`.
