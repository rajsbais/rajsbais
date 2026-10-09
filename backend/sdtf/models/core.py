from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, IdMixin


class User(IdMixin, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    roles: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Project(IdMixin, Base):
    __tablename__ = "projects"
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    scenario_type: Mapped[str] = mapped_column(String(32), nullable=False)  # CARVE_OUT, SDT, MERGER, BLUEFIELD
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class SapSystem(IdMixin, Base):
    __tablename__ = "sap_systems"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    sid: Mapped[str] = mapped_column(String(8), nullable=False)
    client: Mapped[str] = mapped_column(String(3), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # SOURCE / TARGET
    product: Mapped[str] = mapped_column(String(32), nullable=False)  # ECC / S4HANA
    release: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. 6.0 EHP8, 2025
    database: Mapped[str] = mapped_column(String(32), default="")
    os_name: Mapped[str] = mapped_column(String(32), default="")
    connector: Mapped[str] = mapped_column(String(32), default="SYNTHETIC")  # SYNTHETIC | RFC | ODATA | CDS
    connector_status: Mapped[str] = mapped_column(String(32), default="SIMULATED")
    logical_system: Mapped[str] = mapped_column(String(16), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class SapRecord(Base):
    """Generic SAP-like record store used for the synthetic source and the simulated target.

    Production extraction adapters stage into object storage; this relational store exists so that the
    vertical slice is executable end to end without SAP credentials.
    """

    __tablename__ = "sap_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system_id: Mapped[str] = mapped_column(ForeignKey("sap_systems.id"), nullable=False)
    table_name: Mapped[str] = mapped_column(String(30), nullable=False)
    record_key: Mapped[str] = mapped_column(String(200), nullable=False)
    bukrs: Mapped[str | None] = mapped_column(String(4), nullable=True)
    werks: Mapped[str | None] = mapped_column(String(4), nullable=True)
    gjahr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (
        UniqueConstraint("system_id", "table_name", "record_key", name="uq_sap_record"),
        Index("ix_sap_records_sys_table", "system_id", "table_name"),
        Index("ix_sap_records_sys_table_bukrs", "system_id", "table_name", "bukrs"),
    )


class DiscoverySnapshot(IdMixin, Base):
    __tablename__ = "discovery_snapshots"
    system_id: Mapped[str] = mapped_column(ForeignKey("sap_systems.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="COMPLETE")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)


class OrgUnit(IdMixin, Base):
    __tablename__ = "org_units"
    system_id: Mapped[str] = mapped_column(ForeignKey("sap_systems.id"), nullable=False, index=True)
    unit_type: Mapped[str] = mapped_column(String(32), nullable=False)  # COMPANY_CODE, PLANT, SALES_ORG...
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="")
    parent_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)


class TableStatistic(IdMixin, Base):
    __tablename__ = "table_statistics"
    system_id: Mapped[str] = mapped_column(ForeignKey("sap_systems.id"), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("discovery_snapshots.id"), nullable=False, index=True)
    table_name: Mapped[str] = mapped_column(String(30), nullable=False)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    est_bytes: Mapped[int] = mapped_column(Integer, default=0)
    by_company_code: Mapped[dict] = mapped_column(JSON, default=dict)
    by_fiscal_year: Mapped[dict] = mapped_column(JSON, default=dict)
    key_fields: Mapped[list] = mapped_column(JSON, default=list)
    fields: Mapped[list] = mapped_column(JSON, default=list)


class BusinessObjectInstance(Base):
    __tablename__ = "business_object_instances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system_id: Mapped[str] = mapped_column(ForeignKey("sap_systems.id"), nullable=False)
    object_type: Mapped[str] = mapped_column(String(48), nullable=False)
    object_key: Mapped[str] = mapped_column(String(120), nullable=False)
    bukrs: Mapped[str | None] = mapped_column(String(4), nullable=True)
    werks: Mapped[str | None] = mapped_column(String(4), nullable=True)
    gjahr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # OPEN / CLOSED for documents
    company_codes: Mapped[list] = mapped_column(JSON, default=list)  # all company codes the object touches
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("system_id", "object_type", "object_key", name="uq_bo_instance"),
        Index("ix_bo_sys_type", "system_id", "object_type"),
        Index("ix_bo_sys_type_bukrs", "system_id", "object_type", "bukrs"),
    )


class GraphNode(Base):
    __tablename__ = "graph_nodes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system_id: Mapped[str] = mapped_column(ForeignKey("sap_systems.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(160), nullable=False)
    node_type: Mapped[str] = mapped_column(String(48), nullable=False)
    label: Mapped[str] = mapped_column(String(200), default="")
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("system_id", "node_id", name="uq_graph_node"), Index("ix_gn_sys_type", "system_id", "node_type"))


class GraphEdge(Base):
    __tablename__ = "graph_edges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system_id: Mapped[str] = mapped_column(ForeignKey("sap_systems.id"), nullable=False)
    from_node: Mapped[str] = mapped_column(String(160), nullable=False)
    to_node: Mapped[str] = mapped_column(String(160), nullable=False)
    edge_type: Mapped[str] = mapped_column(String(32), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        Index("ix_ge_sys_from", "system_id", "from_node"),
        Index("ix_ge_sys_to", "system_id", "to_node"),
    )


class ScopeManifest(IdMixin, Base):
    """Versioned, immutable execution manifest. Content is hashed; state transitions only."""

    __tablename__ = "scope_manifests"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    definition: Mapped[dict] = mapped_column(JSON, nullable=False)
    impact: Mapped[dict] = mapped_column(JSON, default=dict)
    selection: Mapped[dict] = mapped_column(JSON, default=dict)  # object ids by type, classification, traces
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("project_id", "name", "version", name="uq_manifest_version"),)


class RuleSet(IdMixin, Base):
    __tablename__ = "rule_sets"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    compiled: Mapped[dict] = mapped_column(JSON, default=dict)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (UniqueConstraint("project_id", "name", "version", name="uq_ruleset_version"),)


class MigrationRun(IdMixin, Base):
    __tablename__ = "migration_runs"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    manifest_id: Mapped[str] = mapped_column(ForeignKey("scope_manifests.id"), nullable=False)
    ruleset_id: Mapped[str] = mapped_column(ForeignKey("rule_sets.id"), nullable=False)
    source_system_id: Mapped[str] = mapped_column(ForeignKey("sap_systems.id"), nullable=False)
    target_system_id: Mapped[str] = mapped_column(ForeignKey("sap_systems.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="SIMULATED", nullable=False)  # SIMULATED | REHEARSAL | PRODUCTION
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    started_by: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snapshot_id: Mapped[str] = mapped_column(String(32), default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    stages: Mapped[list[RunStage]] = relationship(back_populates="run", cascade="all, delete-orphan", order_by="RunStage.sequence")


class RunStage(IdMixin, Base):
    __tablename__ = "run_stages"
    run_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    run: Mapped[MigrationRun] = relationship(back_populates="stages")


class StagedRecord(Base):
    __tablename__ = "staged_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id"), nullable=False)
    partition: Mapped[str] = mapped_column(String(64), nullable=False)
    table_name: Mapped[str] = mapped_column(String(30), nullable=False)
    record_key: Mapped[str] = mapped_column(String(200), nullable=False)
    source_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    target_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    target_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lineage: Mapped[list] = mapped_column(JSON, default=list)
    load_status: Mapped[str] = mapped_column(String(16), default="STAGED")
    __table_args__ = (
        UniqueConstraint("run_id", "table_name", "record_key", name="uq_staged"),
        Index("ix_staged_run_table", "run_id", "table_name"),
    )


class TransformationException(IdMixin, Base):
    __tablename__ = "transformation_exceptions"
    run_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id"), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    table_name: Mapped[str] = mapped_column(String(30), default="")
    record_key: Mapped[str] = mapped_column(String(200), default="")
    rule_id: Mapped[str] = mapped_column(String(64), default="")
    severity: Mapped[str] = mapped_column(String(8), default="ERROR")
    message: Mapped[str] = mapped_column(Text, default="")
    disposition: Mapped[str] = mapped_column(String(16), default="OPEN")


class ReconciliationResult(IdMixin, Base):
    __tablename__ = "reconciliation_results"
    run_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id"), nullable=False, index=True)
    layer: Mapped[str] = mapped_column(String(16), nullable=False)  # TECHNICAL / FUNCTIONAL / FINANCIAL
    check_name: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(8), nullable=False)  # PASS / FAIL / WARN
    source_value: Mapped[str] = mapped_column(String(120), default="")
    target_value: Mapped[str] = mapped_column(String(120), default="")
    variance: Mapped[str] = mapped_column(String(120), default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class ApprovalRecord(IdMixin, Base):
    __tablename__ = "approvals"
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)  # MANIFEST / RULESET / RUN / RECONCILIATION
    subject_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="TECHNICAL")  # TECHNICAL / BUSINESS
    comment: Mapped[str] = mapped_column(Text, default="")


class AuditEvent(Base):
    """Append-only, hash-chained audit trail."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AgentDecision(IdMixin, Base):
    __tablename__ = "agent_decisions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), default="")
    subject_id: Mapped[str] = mapped_column(String(64), default="")
    proposal: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED")  # PROPOSED / ACCEPTED / REJECTED
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)


class ExtractionJob(IdMixin, Base):
    """Claimable unit of distributed extraction: one partition of one run. Workers lease jobs with an expiry so that
    a crashed worker's job is re-queued; attempts are counted for back-off and alerting."""

    __tablename__ = "extraction_jobs"
    run_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id"), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(16), default="EXTRACT", nullable=False)  # EXTRACT / TRANSFORM / LOAD
    partition_id: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(48), nullable=False)
    est_rows: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED", nullable=False)  # QUEUED / CLAIMED / DONE / FAILED
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    records: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("run_id", "stage", "partition_id", name="uq_extraction_job"), Index("ix_jobs_status", "status", "lease_until"))


class SourceChangeEvent(Base):
    """Change log of a simulated source (what a real system exposes through Z_SDTF_CDC_POLL). One row per changed
    table row; CHANGENR groups the rows of one business change so replay can treat a document atomically."""

    __tablename__ = "source_change_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system_id: Mapped[str] = mapped_column(ForeignKey("sap_systems.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    changenr: Mapped[str] = mapped_column(String(32), nullable=False)
    object_type: Mapped[str] = mapped_column(String(48), default="")
    table_name: Mapped[str] = mapped_column(String(30), nullable=False)
    record_key: Mapped[str] = mapped_column(String(200), nullable=False)
    op: Mapped[str] = mapped_column(String(1), nullable=False)  # I / U / D
    changed_at: Mapped[str] = mapped_column(String(14), nullable=False)  # YYYYMMDDHHMMSS (UTC)
    changed_by: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    __table_args__ = (UniqueConstraint("system_id", "seq", name="uq_change_seq"),)


class DeltaEvent(IdMixin, Base):
    """A captured change event and what the delta engine did with it (audit trail + idempotency ledger)."""

    __tablename__ = "delta_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id"), nullable=False, index=True)  # the delta cycle
    baseline_run_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    changenr: Mapped[str] = mapped_column(String(32), default="")
    object_type: Mapped[str] = mapped_column(String(48), default="")
    object_key: Mapped[str] = mapped_column(String(200), default="")
    table_name: Mapped[str] = mapped_column(String(30), nullable=False)
    record_key: Mapped[str] = mapped_column(String(200), nullable=False)
    op: Mapped[str] = mapped_column(String(1), nullable=False)
    changed_at: Mapped[str] = mapped_column(String(14), default="")
    changed_by: Mapped[str] = mapped_column(String(64), default="")
    source_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    target_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    target_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="CAPTURED", nullable=False)  # CAPTURED / FILTERED / REJECTED / APPLIED / SKIPPED_DUPLICATE / SKIPPED_MISSING / CONFLICT
    action: Mapped[str] = mapped_column(String(16), default="")  # INSERTED / UPDATED / DELETED / REVERSED / REPOSTED / BLOCKED / DERIVED / MATCHED
    load_method: Mapped[str] = mapped_column(String(32), default="")  # API / CONFIG_TRANSPORT / MIGRATION_COCKPIT / ...
    api_call: Mapped[str] = mapped_column(String(160), default="")  # e.g. API_SALES_ORDER_SRV POST A_SalesOrder (deep insert)
    message: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("baseline_run_id", "seq", name="uq_delta_event_seq"), Index("ix_delta_target", "baseline_run_id", "table_name", "target_key"))


class CockpitTemplate(IdMixin, Base):
    """A migration object template of the target (the XML workbook the *Migrate Your Data* app provides per object
    and release), stored per project and business object so the cockpit export can fill it instead of the generic
    workbook. The parsed structure (sheets, field list) is cached in `structure`; `mapping` holds the explicit
    column overrides an architect recorded for the automatic mapping."""

    __tablename__ = "cockpit_templates"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(48), nullable=False)
    migration_object: Mapped[str] = mapped_column(String(120), default="")
    filename: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    structure: Mapped[dict] = mapped_column(JSON, default=dict)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    uploaded_by: Mapped[str] = mapped_column(String(64), default="")
    __table_args__ = (UniqueConstraint("project_id", "object_type", name="uq_cockpit_template"),)


class CockpitAlias(IdMixin, Base):
    """A template field name learned for a project: `alias` (as the migration object template names the field) ->
    DDIC `field`, proposed from a template's Field List (description match) and confirmed by an architect. Project
    aliases are consulted before the global catalogue (`catalog/fields.py`)."""

    __tablename__ = "cockpit_aliases"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(60), nullable=False)
    field: Mapped[str] = mapped_column(String(30), nullable=False)
    table_name: Mapped[str] = mapped_column(String(30), default="")  # restrict to one table ("" = any table carrying the field)
    description: Mapped[str] = mapped_column(String(200), default="")
    evidence: Mapped[str] = mapped_column(String(200), default="")  # how it was proposed
    object_type: Mapped[str] = mapped_column(String(48), default="")
    status: Mapped[str] = mapped_column(String(12), default="PROPOSED")  # PROPOSED | CONFIRMED | REJECTED
    created_by: Mapped[str] = mapped_column(String(64), default="")
    decided_by: Mapped[str] = mapped_column(String(64), default="")
    __table_args__ = (UniqueConstraint("project_id", "alias", "table_name", name="uq_cockpit_alias"),)


class MigrationObjectEntry(IdMixin, Base):
    """A migration object as the target shows it (imported from the app's object list or a release's documentation):
    release, documented name and technical ID, with the business objects and staging tables it serves. Project
    entries win over the catalogue (`catalog/migration_objects.py`) for their release."""

    __tablename__ = "migration_object_registry"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    release: Mapped[str] = mapped_column(String(32), default="*")  # normalised release or "*"
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    object_id: Mapped[str] = mapped_column(String(60), default="")  # technical ID (SIF_...)
    object_types: Mapped[list] = mapped_column(JSON, default=list)
    tables: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(String(400), default="")
    source: Mapped[str] = mapped_column(String(200), default="")
    imported_by: Mapped[str] = mapped_column(String(64), default="")
    __table_args__ = (UniqueConstraint("project_id", "release", "name", name="uq_migration_object_entry"),)


class CockpitFeedback(IdMixin, Base):
    """One message of the migration cockpit's upload simulation log, matched to the exported instance it belongs
    to (`matched_key` = the instance key as the export groups it) or recorded as unmatched with the reason."""

    __tablename__ = "cockpit_feedback"
    run_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id"), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(48), default="")
    migration_object: Mapped[str] = mapped_column(String(120), default="")
    instance_key: Mapped[str] = mapped_column(String(200), default="")
    matched_key: Mapped[str] = mapped_column(String(200), default="")
    matched: Mapped[bool] = mapped_column(Boolean, default=False)
    severity: Mapped[str] = mapped_column(String(1), default="I")  # E | W | S | I
    message: Mapped[str] = mapped_column(Text, default="")
    message_class: Mapped[str] = mapped_column(String(40), default="")
    message_number: Mapped[str] = mapped_column(String(10), default="")
    sheet: Mapped[str] = mapped_column(String(120), default="")
    field: Mapped[str] = mapped_column(String(60), default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    reason: Mapped[str] = mapped_column(String(300), default="")
    source_file: Mapped[str] = mapped_column(String(200), default="")
    imported_by: Mapped[str] = mapped_column(String(64), default="")
    attempt_id: Mapped[str] = mapped_column(String(32), default="")  # the round (CockpitAttempt) the log answers
    attempt_sequence: Mapped[int] = mapped_column(Integer, default=0)


class CockpitAttempt(IdMixin, Base):
    """One migration cockpit package round of a run: the full export or a retry package, with the manual upload
    and migration steps recorded by hand and the simulation outcome per instance once the feedback is imported."""

    __tablename__ = "cockpit_attempts"
    run_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(String(12), default="all")  # all | rejected
    status: Mapped[str] = mapped_column(String(12), default="EXPORTED")  # EXPORTED | UPLOADED | SIMULATED | MIGRATED | SUPERSEDED
    package_dir: Mapped[str] = mapped_column(String(400), default="")
    zip_path: Mapped[str] = mapped_column(String(400), default="")
    manifest_sha256: Mapped[str] = mapped_column(String(64), default="")
    instances: Mapped[int] = mapped_column(Integer, default=0)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    files: Mapped[int] = mapped_column(Integer, default=0)
    instance_keys: Mapped[list] = mapped_column(JSON, default=list)  # [[object_type, instance_key], ...]
    outcomes: Mapped[dict] = mapped_column(JSON, default=dict)  # {"object_type|key": accepted|rejected|not_in_log}
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exported_by: Mapped[str] = mapped_column(String(64), default="")
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(64), default="")
    upload_note: Mapped[str] = mapped_column(String(400), default="")
    simulated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    simulated_by: Mapped[str] = mapped_column(String(64), default="")
    migrated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    migrated_by: Mapped[str] = mapped_column(String(64), default="")
    migration_note: Mapped[str] = mapped_column(String(400), default="")
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_cockpit_attempt"),)
