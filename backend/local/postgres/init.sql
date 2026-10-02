-- Day 1 schema (docs/plan.md Day 1, docs/ARCHITECTURE.md §8).
-- Runs once, on first start of an empty Postgres volume. `apps` (Day 5) and
-- `outbox` (Day 15) are added by the days that introduce them.

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

CREATE INDEX IF NOT EXISTS cases_app_key_idx ON cases (app_id, alert_key);
CREATE INDEX IF NOT EXISTS cases_app_status_idx ON cases (app_id, status);

-- Durable snapshot on every memory-store update; Redis's
-- rebuild-from-source-of-truth path (§6). Snapshot fields mirror
-- memory_store.proto ContextAggregate.
CREATE TABLE IF NOT EXISTS memory_history (
    id                       BIGSERIAL PRIMARY KEY,
    app_id                   TEXT        NOT NULL,
    alert_key                TEXT        NOT NULL,
    "window"                 TEXT        NOT NULL
                             CHECK ("window" IN ('1h', '24h', '7d')),
    alert_count              INTEGER     NOT NULL DEFAULT 0,
    escalation_count         INTEGER     NOT NULL DEFAULT 0,
    suppression_count        INTEGER     NOT NULL DEFAULT 0,
    confirmed_incident_count INTEGER     NOT NULL DEFAULT 0,
    confirmed_noise_count    INTEGER     NOT NULL DEFAULT 0,
    recorded_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_history_lookup_idx
    ON memory_history (app_id, alert_key, "window", recorded_at DESC);
