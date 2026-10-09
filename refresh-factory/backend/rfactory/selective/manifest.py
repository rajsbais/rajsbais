"""Versioned, hash-addressed execution manifest."""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

from pydantic import BaseModel, Field


class Scope(BaseModel):
    object_type: str = "SALES_ORDER"
    company_codes: list[str] = Field(default_factory=list)
    plants: list[str] = Field(default_factory=list)
    sales_orgs: list[str] = Field(default_factory=list)
    customers: list[str] = Field(default_factory=list)
    carriers: list[str] = Field(default_factory=list)  # airlines (flight demo model)
    vendors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    document_types: list[str] = Field(default_factory=list)
    fiscal_years: list[str] = Field(default_factory=list)
    explicit_keys: list[str] = Field(default_factory=list)  # header keys, composite joined by "/"
    date_from: date | None = None
    date_to: date | None = None

    @classmethod
    def last_days(cls, days: int, today: date, **kw) -> "Scope":
        return cls(date_from=today - timedelta(days=days), date_to=today, **kw)

    def dims(self) -> dict[str, list[str]]:
        return {k: getattr(self, k) for k in
                ("company_codes", "plants", "sales_orgs", "customers", "carriers", "vendors", "materials",
                 "document_types", "fiscal_years") if getattr(self, k)}


class Manifest(BaseModel):
    name: str = "selective-refresh"
    version: int = 1
    source_system_id: str
    target_system_id: str
    scope: Scope
    include_downstream: list[str] = Field(default_factory=list)  # e.g. DELIVERY, BILLING, FI_DOCUMENT
    masking_policy_id: str = "gdpr-standard"
    conflict_policy: dict[str, str] = Field(default_factory=dict)  # conflict type -> action
    instance_overrides: dict[str, str] = Field(default_factory=dict)  # instance id -> action
    approved_exceptions: dict[str, str] = Field(default_factory=dict)  # instance id -> justification

    def content_hash(self) -> str:
        """Hash of everything that changes *what* is executed (not name/version)."""
        payload = self.model_dump(mode="json", exclude={"name", "version"})
        if not payload["scope"].get("carriers"):
            payload["scope"].pop("carriers", None)  # a field added later: manifests that do not use it keep the hash they were approved under
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()
