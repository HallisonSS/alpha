-- KILN: tabelas do Agent Control Plane. Idempotente (pode rodar quantas vezes quiser).
-- Requer PostgreSQL 13+ (gen_random_uuid nativo).
-- Aplicar: docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < db/kiln.sql

CREATE TABLE IF NOT EXISTS kiln_runs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    goal         text        NOT NULL,
    status       text        NOT NULL DEFAULT 'created',
    plan         jsonb,
    budget       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    config       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_by   text        NOT NULL DEFAULT 'api',
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    CONSTRAINT kiln_runs_status_chk CHECK (status IN
      ('created','planning','awaiting_plan_approval','starting','running','integrating',
       'succeeded','failed','cancelled','budget_exceeded'))
);
CREATE INDEX IF NOT EXISTS kiln_runs_status_idx ON kiln_runs (status);

CREATE TABLE IF NOT EXISTS kiln_tasks (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         uuid        NOT NULL REFERENCES kiln_runs(id) ON DELETE CASCADE,
    agent          text        NOT NULL,
    objective      text        NOT NULL DEFAULT '',
    owned_paths    text[]      NOT NULL DEFAULT '{}',
    depends_on     text[]      NOT NULL DEFAULT '{}',
    acceptance     text[]      NOT NULL DEFAULT '{}',
    status         text        NOT NULL DEFAULT 'pending',
    attempts       int         NOT NULL DEFAULT 0,
    branch         text,
    state          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    result         jsonb,
    baseline_tests int         NOT NULL DEFAULT 0,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, agent),
    CONSTRAINT kiln_tasks_status_chk CHECK (status IN
      ('pending','running','awaiting_approval','blocked_contract','resume',
       'agent_done','verifying','merged','failed'))
);
CREATE INDEX IF NOT EXISTS kiln_tasks_run_idx ON kiln_tasks (run_id, status);

CREATE TABLE IF NOT EXISTS kiln_contracts (
    id          bigserial PRIMARY KEY,
    run_id      uuid        NOT NULL REFERENCES kiln_runs(id) ON DELETE CASCADE,
    path        text        NOT NULL,
    version     int         NOT NULL,
    sha256      text        NOT NULL,
    content     text        NOT NULL,
    active      boolean     NOT NULL DEFAULT true,
    approved_by text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, path, version)
);
CREATE INDEX IF NOT EXISTS kiln_contracts_active_idx ON kiln_contracts (run_id) WHERE active;

-- Auditoria: entrada, decisao, comando, saida. Append-only (trigger abaixo).
CREATE TABLE IF NOT EXISTS kiln_events (
    id      bigserial PRIMARY KEY,
    run_id  uuid        NOT NULL,
    task_id uuid,
    ts      timestamptz NOT NULL DEFAULT now(),
    actor   text        NOT NULL,
    type    text        NOT NULL,
    data    jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS kiln_events_run_idx ON kiln_events (run_id, id);
CREATE INDEX IF NOT EXISTS kiln_events_type_idx ON kiln_events (type, ts);

CREATE OR REPLACE FUNCTION kiln_events_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'kiln_events e append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS kiln_events_no_update ON kiln_events;
CREATE TRIGGER kiln_events_no_update BEFORE UPDATE ON kiln_events
    FOR EACH ROW EXECUTE FUNCTION kiln_events_immutable();
-- DELETE so e bloqueado fora de TRUNCATE/ON DELETE CASCADE de manutencao: sem FK em run_id de proposito.
DROP TRIGGER IF EXISTS kiln_events_no_delete ON kiln_events;
CREATE TRIGGER kiln_events_no_delete BEFORE DELETE ON kiln_events
    FOR EACH ROW EXECUTE FUNCTION kiln_events_immutable();

CREATE TABLE IF NOT EXISTS kiln_approvals (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id       uuid        NOT NULL REFERENCES kiln_runs(id) ON DELETE CASCADE,
    task_id      uuid        REFERENCES kiln_tasks(id) ON DELETE CASCADE,
    kind         text        NOT NULL,
    status       text        NOT NULL DEFAULT 'pending',
    request      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    decided_by   text,
    note         text,
    applied_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    decided_at   timestamptz,
    CONSTRAINT kiln_approvals_kind_chk CHECK (kind IN ('plan','contract_change','config_change','command')),
    CONSTRAINT kiln_approvals_status_chk CHECK (status IN ('pending','approved','denied'))
);
CREATE INDEX IF NOT EXISTS kiln_approvals_pending_idx ON kiln_approvals (status, created_at);
CREATE INDEX IF NOT EXISTS kiln_approvals_run_idx ON kiln_approvals (run_id, kind);

CREATE TABLE IF NOT EXISTS kiln_usage (
    id         bigserial PRIMARY KEY,
    run_id     uuid        NOT NULL REFERENCES kiln_runs(id) ON DELETE CASCADE,
    task_id    uuid,
    ts         timestamptz NOT NULL DEFAULT now(),
    kind       text        NOT NULL,
    tokens_in  int         NOT NULL DEFAULT 0,
    tokens_out int         NOT NULL DEFAULT 0,
    cost_usd   numeric(12,6) NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS kiln_usage_run_idx ON kiln_usage (run_id);

CREATE TABLE IF NOT EXISTS kiln_lessons (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id     uuid        REFERENCES kiln_runs(id) ON DELETE SET NULL,
    task_id    uuid,
    text       text        NOT NULL,
    status     text        NOT NULL DEFAULT 'proposed',
    decided_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    CONSTRAINT kiln_lessons_status_chk CHECK (status IN ('proposed','approved','rejected'))
);
CREATE INDEX IF NOT EXISTS kiln_lessons_status_idx ON kiln_lessons (status);

-- Views para Grafana (datasource Postgres opcional).
CREATE OR REPLACE VIEW kiln_v_run_usage AS
SELECT r.id AS run_id, r.status, r.created_at,
       COALESCE(SUM(u.tokens_in + u.tokens_out), 0) AS tokens,
       COALESCE(SUM(u.cost_usd), 0)                 AS cost_usd
FROM kiln_runs r LEFT JOIN kiln_usage u ON u.run_id = r.id
GROUP BY r.id;

CREATE OR REPLACE VIEW kiln_v_policy_denials AS
SELECT run_id, task_id, ts, data->>'rule' AS rule, data->>'reason' AS reason
FROM kiln_events WHERE type = 'policy.deny';
