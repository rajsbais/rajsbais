"""Honest capability matrix. `status` vocabulary:
  implemented-simulated : real logic, exercised by automated tests, but only against the synthetic SAP adapter
  designed              : documented design, no code
  catalogued            : data/catalog only, no execution engine
  planned               : not started
Nothing in this MVP is `production-ready against real SAP`.
"""

CAPABILITIES = [
    ("M1 Landscape discovery", "System registry, readiness, refresh-combination matrix", "implemented-simulated",
     "ECC 6.0 EHP8 and S/4HANA 2023 (both simulated)", "Static metadata; no RFC/ABAP agent", "tests/test_landscape.py", "RFC/OData adapter, DD02L/DD03L metadata, DB sizing"),
    ("M2 Full system refresh", "13-phase runbook model, production-target guard", "catalogued",
     "n/a", "No execution engine; SWPM/HANA/snapshot adapters absent", "tests/test_landscape.py::test_full_refresh_guard", "Infrastructure adapters, SWPM orchestration"),
    ("M3 Post-copy factory", "11-task catalog with pre/post-check, rollback, evidence, approval fields", "catalogued",
     "ECC/S4 (catalog)", "Tasks not executable", "tests/test_landscape.py", "Executable task adapters per SAP release"),
    ("M4 Lean client builder", "Shell/lean-client provisioning", "planned", "-", "-", "-", "Everything"),
    ("M5 Selective data copy", "Manifest, scope filters, preview, volume estimate", "implemented-simulated",
     "ECC 6.0 EHP8 and S/4HANA 2023 subsets (27 tables incl. BUT000, ACDOCA)", "In-memory adapter; staging is local files", "tests/test_selective_flow.py", "ABAP extraction agent, object store staging, real loaders"),
    ("M6 Dependency engine", "Semantic graph, recursive expansion, cycles, orphans, dangling refs, extension API", "implemented-simulated",
     "SD/MM/FI/MD objects for ECC and S/4 (BP, ACDOCA); no PP/QM/PM/PS/WM, no S/4 MATDOC", "Relationships are hand-modelled", "tests/test_dependency.py", "More objects, learned relationships from DDIC/change docs"),
    ("M7 Masking", "Catalog + pattern discovery, FPE-style substitution, pseudonymize/anonymize/tokenize, coverage evidence", "implemented-simulated",
     "Field-level, SAP subset", "Not NIST FF1/FF3; no HSM/KMS", "tests/test_masking.py", "KMS/HSM, vetted FPE, payroll/HR objects, in-DB masking"),
    ("M8 Delta refresh", "Approved standing scenarios, multi-object rolling scopes, change-log/created-only/full-compare mechanisms, full sweeps, watermarks, target-drift detection, deferred/stale retry, resume/rollback, weekly/interval schedule with window and external-scheduler tick", "implemented-simulated",
     "ECC and S/4HANA (simulated change log)", "Change log is a simulation of CDHDR/CDPOS; no real change-document reader; target drift covers rows we loaded; source deletions reported, never propagated; scheduler is pull-based (tick), no built-in daemon", "tests/test_delta.py, tests/test_api.py::test_delta_refresh_over_http", "Real change-document readers per object class, durable scheduler, parallel packages, per-object mechanism profiles from metadata, S/4 MATDOC/ACDOCA delta specifics"),
    ("M9 TDM factory", "Catalog, scenario templates, synthetic generation", "planned", "-", "-", "-", "Everything"),
    ("M10 Conflict management", "Duplicate/identical/different, config gaps, number ranges, cascade, ownership, policies", "implemented-simulated",
     "Document and master objects", "REMAP not implemented; UPDATE only for master data", "tests/test_conflicts.py", "Remap, richer ownership model"),
    ("M11 Reconciliation", "Technical, business and security checks, release gate, evidence ZIP", "implemented-simulated",
     "SD/FI/MD checks", "Checks cover loaded scope only", "tests/test_selective_flow.py, test_reconcile.py, test_s4hana.py", "Inventory/open-item/valuation checks, PP/MM-IM"),
    ("M12 AI agents", "Strategy, masking, conflict-policy and duration advisors (rule-based)", "implemented-simulated",
     "-", "No LLM; duration model uses placeholder throughput", "tests/test_advisors.py", "LLM agents with tool access under policy engine; calibrated predictors"),
    ("M13 Control tower UI", "React/TS: dashboard, landscape, designer, dependencies, masking, conflicts, execution, reconciliation, audit, copilot", "implemented-simulated",
     "-", "Subset of the 16 screens, merged into 9 views", "npm run build; manual API contract", "Remaining screens, RBAC-aware nav, e2e tests"),
    ("M14 Orchestration", "Checkpoint, retry, resume, rollback inside one run", "implemented-simulated",
     "-", "No queues, scheduler, parallelism, maintenance windows", "tests/test_execution.py", "Durable workflow engine (Temporal), scheduler integration"),
    ("M15 Security", "Roles, SoD, agent restrictions, hash-chained audit, prod read-only view", "implemented-simulated",
     "-", "Demo-header auth; no SSO/ABAC/tenancy/encryption at rest", "tests/test_governance.py", "OIDC/SAML, ABAC, KMS, WORM audit sink"),
]
