-- Day 1 schema (docs/plan.md Day 1, docs/ARCHITECTURE.md §8).
-- Runs once, on first start of an empty Postgres volume. Tables are added by
-- the day that introduces them (`tools`/`apps`: Day 5, `memory_events`: Day 6,
-- `outbox`: Day 15); on
-- an existing volume, apply a new table's block by hand or `down -v`.
-- Every statement is idempotent, so re-running this file is safe.

-- Only ESCALATE decisions land here (§4 step 4). Every read filters by app_id.
CREATE TABLE IF NOT EXISTS cases (
    id               BIGSERIAL PRIMARY KEY,
    app_id           TEXT        NOT NULL,
    alert_id         TEXT        NOT NULL,
    alert_key        TEXT        NOT NULL,
    decision         TEXT        NOT NULL,
    reasons          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    status           TEXT        NOT NULL DEFAULT 'OPEN'
                     CHECK (status IN ('OPEN', 'RESOLVED')),
    verdict          TEXT,
    verdict_by       TEXT,
    -- The analyst's account of the actual fix (§6).
    resolution_notes TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    -- A duplicate alert.decided (§10) must never create a second case.
    UNIQUE (app_id, alert_id)
);

-- Day 9: the rest of RunAgentResponse that a case shows the analyst
-- (ALTER, so it also applies to a volume created before Day 9).
ALTER TABLE cases
    ADD COLUMN IF NOT EXISTS agent_id   TEXT  NOT NULL DEFAULT '',
    -- [{tool_name, result_summary, agent_id}], from RunAgentResponse.tool_calls.
    ADD COLUMN IF NOT EXISTS tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS confidence REAL  NOT NULL DEFAULT 0,
    -- The alert as orchestrator received it (RunAgentResponse.alert,
    -- ADR-0023): {source, severity, message, fired_at, payload}. NULL for a
    -- decision published before it carried one.
    ADD COLUMN IF NOT EXISTS alert      JSONB;

CREATE INDEX IF NOT EXISTS cases_app_key_idx ON cases (app_id, alert_key);
CREATE INDEX IF NOT EXISTS cases_app_status_idx ON cases (app_id, status);

-- memory-store's source of truth (ADR-0017, §6, §8): one row per event.
-- kind: "decision:<Decision>" (from alert.decided, ADR-0016) or, from Day 10,
-- "verdict:<verdict>". ref_id: the alert_id (decision) or case_id (verdict).
-- Unique per (app_id, family, ref_id): a redelivered event is stored once.
-- Redis caches the last 7 days per alert_key and is rebuilt from here.
CREATE TABLE IF NOT EXISTS memory_events (
    id          BIGSERIAL PRIMARY KEY,
    app_id      TEXT        NOT NULL,
    alert_key   TEXT        NOT NULL,
    kind        TEXT        NOT NULL CHECK (kind ~ '^(decision|verdict):[A-Z_]+$'),
    family      TEXT        GENERATED ALWAYS AS (split_part(kind, ':', 1)) STORED,
    ref_id      TEXT        NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (app_id, family, ref_id)
);

CREATE INDEX IF NOT EXISTS memory_events_lookup_idx
    ON memory_events (app_id, alert_key, occurred_at DESC);

-- registry's tool registrations (Day 5). One row per tool version.
-- read_only must be true: no tool may change external state (ADR-0004, §13 T8).
-- enabled is a runtime switch: a disabled tool is offered to no agent.
CREATE TABLE IF NOT EXISTS tools (
    tool_id       TEXT        NOT NULL,
    version       TEXT        NOT NULL,
    description   TEXT        NOT NULL,
    scope         TEXT        NOT NULL CHECK (scope IN ('app', 'global')),
    -- The owning app for scope='app'; NULL for global tools.
    app_id        TEXT,
    input_schema  JSONB       NOT NULL,
    output_schema JSONB,
    read_only     BOOLEAN     NOT NULL CHECK (read_only),
    enabled       BOOLEAN     NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tool_id, version),
    CHECK ((scope = 'app') = (app_id IS NOT NULL))
);

-- registry's App Manifest store (§3, §8): one row per app, the manifest kept
-- whole as jsonb. The runtime copy; the source of truth is
-- backend/apps/{app_id}/manifest.yaml (ADR-0006).
CREATE TABLE IF NOT EXISTS apps (
    app_id       TEXT        PRIMARY KEY,
    display_name TEXT        NOT NULL,
    manifest     JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
