-- PostgreSQL schema for the production persistence layer (DESIGN ARTEFACT: NOT executed or tested by the MVP,
-- which keeps state in memory and the audit log in JSONL). Multi-tenant via tenant_id on every table + row-level security.
CREATE TABLE sap_system (
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL, sid text NOT NULL, client text NOT NULL, role text NOT NULL CHECK (role IN ('PRD','QAS','DEV','UAT','SBX','TRN')),
  product text NOT NULL, family text NOT NULL CHECK (family IN ('ECC','S4')), release text, db_type text, owner text,
  adapter text NOT NULL, writable_target_allowed boolean NOT NULL DEFAULT true,
  UNIQUE (tenant_id, sid, client));
CREATE TABLE refresh_project (
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL, name text NOT NULL, source_id uuid NOT NULL REFERENCES sap_system, target_id uuid NOT NULL REFERENCES sap_system,
  status text NOT NULL, created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE manifest_version (
  project_id uuid REFERENCES refresh_project, version int, content_hash char(64) NOT NULL, body jsonb NOT NULL, created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (project_id, version));
CREATE TABLE approval (
  id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES refresh_project, manifest_hash char(64) NOT NULL, approved_by text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('plan','exception')), subject text, justification text, approved_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE run (
  id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES refresh_project, manifest_hash char(64) NOT NULL, status text NOT NULL,
  checkpoint int NOT NULL DEFAULT 0, attempts int NOT NULL DEFAULT 0, release text NOT NULL DEFAULT 'NOT_EVALUATED', started_at timestamptz, finished_at timestamptz);
CREATE TABLE run_step (run_id uuid REFERENCES run, name text, status text, started_at timestamptz, finished_at timestamptz, detail jsonb, PRIMARY KEY (run_id, name));
CREATE TABLE run_undo (run_id uuid REFERENCES run, seq bigserial, table_name text, key jsonb, prior_image jsonb, PRIMARY KEY (run_id, seq)); -- encrypted at rest
CREATE TABLE conflict_finding (id uuid PRIMARY KEY, project_id uuid REFERENCES refresh_project, type text, severity text, instance text, action text, details jsonb);
CREATE TABLE reconciliation_check (run_id uuid REFERENCES run, check_id text, category text CHECK (category IN ('technical','business','security')), status text, detail text, samples jsonb, PRIMARY KEY (run_id, check_id));
CREATE TABLE masking_policy (id text PRIMARY KEY, tenant_id uuid, name text, rules jsonb NOT NULL);
CREATE TABLE audit_log (seq bigserial PRIMARY KEY, tenant_id uuid NOT NULL, ts timestamptz NOT NULL, actor text NOT NULL, action text NOT NULL, resource text NOT NULL, details jsonb, prev char(64) NOT NULL, hash char(64) NOT NULL);
-- append-only: REVOKE UPDATE, DELETE ON audit_log FROM app_role; ship to object-lock storage.
