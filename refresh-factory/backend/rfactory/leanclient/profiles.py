"""Client-copy profile catalog and the three distinct refresh workflows.

The profiles mirror the *idea* of SAP client-copy profiles (customizing only, +master data, +transactions) but they are
this platform's own definitions: the lean builder composes them from a reusable environment template, with masking and
dependency completeness enforced. They are not SAP transaction SCCL profiles and do not call SAP client-copy programs.
"""

PROFILES = [
    {"id": "SHELL", "name": "Client shell", "includes": ["client entry (number, name, role, logical system)"],
     "excludes": ["all customizing", "all data"],
     "use": "Starting point for every build; also usable on its own as a clean target for a later copy."},
    {"id": "CUSTOMIZING_ONLY", "name": "Configuration-only baseline", "includes": ["T001 company codes", "T001W plants", "NRIV number-range intervals (reset)"],
     "excludes": ["master data", "transactional data", "user master (not modelled)"],
     "use": "Configuration-only client baselines; the customizing needed by whatever data is loaded is pulled in automatically."},
    {"id": "CUSTOMIZING_PLUS_MASTER", "name": "Customizing + masked master data", "includes": ["configuration baseline", "customers", "vendors", "materials (masked, size-limited)"],
     "excludes": ["transactional data"], "use": "Sandbox and training clients."},
    {"id": "LEAN_TEST", "name": "Lean test client", "includes": ["configuration baseline", "masked master data", "selective transactional slices (complete business scenarios)"],
     "excludes": ["everything not needed by the selected scenarios"], "use": "Functional and regression test clients."},
    {"id": "TRAINING", "name": "Training client", "includes": ["configuration baseline", "masked master data", "synthetic scenario data (no production transactions)"],
     "excludes": ["real transactional data"], "use": "Training clients: fully predictable, no production documents."},
]

WORKFLOWS = [
    {"id": "client_build", "name": "Lean client build (this module)", "scope": "A NEW client in an existing non-production system",
     "copies": "Configuration baseline + masked master data + selective or synthetic transactions",
     "typical_use": "Training, sandbox, functional and regression clients", "implemented": "simulated"},
    {"id": "selective_copy", "name": "Selective business-object copy / delta refresh", "scope": "Business objects into an EXISTING client",
     "copies": "Selected objects with their dependencies; never customizing", "typical_use": "Keep QA/DEV data fresh", "implemented": "simulated"},
    {"id": "system_copy", "name": "Full system copy / refresh", "scope": "The WHOLE system (all clients, repository, client-independent customizing)",
     "copies": "Everything, via SWPM / HANA backup-restore / snapshots", "typical_use": "Rebasing a system on production",
     "implemented": "catalogued only (no execution engine)"},
]

PURPOSES = {"training": "TRN", "sandbox": "SBX", "functional": "QAS", "regression": "QAS"}
RESERVED_CLIENTS = {"000", "001", "066"}  # SAP delivery clients: never created or overwritten by this tool


def preset(purpose: str, company_code: str = "1000") -> dict:
    """Sensible starting definitions per purpose (not saved; the author reviews and creates them)."""
    cc = [company_code]
    base = {"purpose": purpose, "customizing": {"company_codes": cc}}
    if purpose == "training":
        return {**base, "name": f"Training client (company {company_code})", "masking_policy_id": "gdpr-strict", "protect_after_build": True,
                "retention_days": 90, "max_rows": 600,
                "masters": [{"type": "CUSTOMER", "max": 5}, {"type": "VENDOR", "max": 3}, {"type": "MATERIAL", "max": 6}],
                "transactions": [{"template_id": "o2c_complete", "count": 3, "mode": "synthetic"},
                                 {"template_id": "o2c_order_only", "count": 2, "mode": "synthetic"},
                                 {"template_id": "p2p_purchase_order", "count": 2, "mode": "synthetic"}]}
    if purpose == "sandbox":
        return {**base, "name": f"Sandbox client (company {company_code})", "masking_policy_id": "gdpr-strict", "protect_after_build": False,
                "retention_days": 30, "max_rows": 300,
                "masters": [{"type": "CUSTOMER", "max": 3}, {"type": "VENDOR", "max": 2}, {"type": "MATERIAL", "max": 4}], "transactions": []}
    if purpose == "functional":
        return {**base, "name": f"Functional test client (company {company_code})", "masking_policy_id": "gdpr-standard", "protect_after_build": False,
                "retention_days": 60, "max_rows": 1500,
                "masters": [{"type": "CUSTOMER", "max": 50}, {"type": "VENDOR", "max": 50}, {"type": "MATERIAL", "max": 50}],
                "transactions": [{"template_id": "o2c_complete", "count": 3, "mode": "subset", "days": 90},
                                 {"template_id": "o2c_delivered_unbilled", "count": 2, "mode": "subset", "days": 90},
                                 {"template_id": "p2p_purchase_order", "count": 2, "mode": "subset", "days": 90}]}
    if purpose == "regression":
        return {**base, "name": f"Regression test client (company {company_code})", "masking_policy_id": "gdpr-standard", "protect_after_build": True,
                "retention_days": 180, "max_rows": 3000,
                "masters": [{"type": "CUSTOMER", "max": 100}, {"type": "VENDOR", "max": 100}, {"type": "MATERIAL", "max": 100}],
                "transactions": [{"template_id": "o2c_complete", "count": 5, "mode": "subset", "days": 90},
                                 {"template_id": "o2c_order_only", "count": 3, "mode": "subset", "days": 90},
                                 {"template_id": "o2c_delivered_unbilled", "count": 3, "mode": "subset", "days": 90},
                                 {"template_id": "p2p_purchase_order", "count": 3, "mode": "subset", "days": 90},
                                 {"template_id": "r2r_billing_posting", "count": 2, "mode": "subset", "days": 90}]}
    raise KeyError(purpose)
