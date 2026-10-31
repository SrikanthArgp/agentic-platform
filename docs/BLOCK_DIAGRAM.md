# Platform Block Diagram

A component-level view of the platform. For the day-by-day build sequence
see `docs/plan.md`; for the full design rationale, contracts, and the
multi-app model see `docs/ARCHITECTURE.md` (this doc is the picture,
`ARCHITECTURE.md` is the detail behind each block).

**Reading the diagram**: solid arrows are synchronous calls or event
publishes on the alert path (including the `outbox-relay` worker, which
carries every alert and every verdict to Kafka: `ingestion` and
`review-console` only write outbox rows); dashed arrows are telemetry
(traces/logs/metrics) or supporting links (Redis counters).

**Deployment**: each block is one container (Compose) or one Deployment
(the local kind cluster from `docs/plan.md` Day 18), shared by every app.
Adding an app adds no blocks (`ARCHITECTURE.md` §12, ADR-0014).

**Scope**: everything drawn here is the platform. Alert sources and the
systems they monitor are outside it — they only appear as the external
client posting events. The platform triages; it doesn't detect or
remediate (`ARCHITECTURE.md` §2).

```mermaid
flowchart TB
    subgraph Clients["Clients"]
        EC["External client<br/>(alert source)"]
        AN["Analyst"]
    end

    subgraph Services["Platform Services"]
        ING["ingestion"]
        ORCH["orchestrator"]
        TG["tool-gateway"]
        MS["memory-store"]
        REG["registry"]
        RC["review-console"]
    end

    REL["outbox-relay worker<br/>(drains outbox tables)"]

    subgraph Data["Data Plane"]
        KAFKA[("Kafka")]
        REDIS[("Redis")]
        PG[("Postgres")]
    end

    subgraph Obs["Observability Plane"]
        OTEL["OTel Collector"]
        TEMPO[("Tempo<br/>traces")]
        MIMIR[("Mimir<br/>metrics")]
        LOKI[("Loki<br/>logs")]
        GRAF["Grafana"]
    end

    EC -->|"REST POST /apps/{app_id}/events"| ING
    ING -->|"REST resolve App Manifest"| REG
    ING -->|"outbox write"| PG
    ING <-.->|"rate-limit counters"| REDIS
    KAFKA -->|consume| ORCH
    ORCH -->|"MCP tool-calls"| TG
    ORCH -->|"gRPC GetContext"| MS
    ORCH -->|"REST resolve App Manifest"| REG
    ORCH -->|"Kafka: alert.decided"| KAFKA
    ORCH <-.->|"budgets, dedupe"| REDIS
    TG -->|"REST GET /cases (similar-past-case-lookup)"| RC
    TG -->|"REST agent's allowed tools (per-call check)"| REG
    KAFKA -->|consume| RC
    AN -->|"REST GET/POST cases, verdict"| RC
    KAFKA -->|"consume alert.decided, verdict.recorded"| MS
    MS -->|"REST memory_namespace"| REG

    MS <-->|"last 7 days of events per alert_key"| REDIS
    MS <-->|"memory_events (event log)"| PG
    REG <-->|"App Manifests, tools"| PG
    RC <-->|"cases + verdict_outbox write (one transaction)"| PG

    REL -->|"relay: alert.received / .dlq, verdict.recorded"| KAFKA
    REL <-->|"claim outbox, verdict_outbox rows"| PG

    ING -.-> OTEL
    ORCH -.-> OTEL
    TG -.-> OTEL
    MS -.-> OTEL
    REG -.-> OTEL
    RC -.-> OTEL
    REL -.-> OTEL
    OTEL --> TEMPO
    OTEL --> MIMIR
    OTEL --> LOKI
    TEMPO --> GRAF
    MIMIR --> GRAF
    LOKI --> GRAF
```

---

## Clients

### External client (alert source)
Whatever raises alerts for an app — a monitoring system (it-ops), a cloud
cost-anomaly detector (cost), a SIEM/EDR (security) — or, in this build,
`backend/scripts/simulate_alerts.py` standing in for all three, plus
curl during development. These systems are **outside** the platform: it
neither hosts nor observes them. Each source picks its app by URL
(`POST /apps/{app_id}/events`) — routing is never inferred. Talks only to
`ingestion`, over REST. Never talks to Kafka, `orchestrator`, or any other
internal service directly.

### Analyst
A human reviewing escalated cases through `review-console`'s REST API.
Only sees `ESCALATE` decisions — auto-resolved/suppressed alerts never
reach a person (see `review-console` below).

---

## Platform services

### `ingestion`
The only REST entrypoint for new events. Validates a request against the
target app's `event_schema_ref` (from `registry`'s App Manifest, 30s TTL
cache), writes it to the Postgres `outbox` table, and returns `202` once
that row commits — it does not wait for a decision, and has no Kafka
client. The `outbox-relay` worker publishes the row to `alert.received`
(`DEAD` and a copy on `alert.received.dlq` after 5 rejections of the same
message), so a Kafka outage doesn't
drop or refuse alerts; Postgres down → `503`. Enforces rate limits per `app_id` and
per source within it (fixed one-minute Redis counters, limits from the
manifest's `rate_limit`, `429` + `Retry-After`, ADR-0029), so one app's
flood can't take another's allowance; Redis down → events are accepted.

### `orchestrator`
The agent runtime — the only service that runs LLM tool-calling loops. On
consuming `alert.received`, it resolves the event's `app_id` against
`registry` to get that app's agent config and tool allowlist, checks the
agent's daily token budget (Redis, ADR-0029: over it, the alert is "not
evaluated" and escalates), calls
`memory-store` once for behavioral context (before the loop, over one
shared gRPC channel, ADR-0019), calls the agent's `prefetch_tools` with no
arguments (ADR-0028), runs the agent's tool-calling loop
(LangChain's prebuilt agent inside a LangGraph run graph, ADR-0022) against
`tool-gateway` with that context in the prompt, applies the supervisor
(the agent's confidence capped by fixed rules, against the manifest's
threshold, ADR-0020; an app can require a confirmed-noise verdict on the
key before any `SUPPRESS` stands, ADR-0031) and the manifest's deterministic `escalate_when`
guardrails (both can only raise a decision to `ESCALATE`,
`ARCHITECTURE.md` §5/§13), and publishes `alert.decided` with
`confidence` and `reasons[]` populated from its own tool-call trace and
those checks.
One service runs every app's agents: an agent is a manifest entry (prompt,
tool allowlist, role), and each alert gets an ephemeral run of it — nothing
is spawned per app or per alert.
Also exposes `RunAgent` as a direct gRPC call (same contract, synchronous
path) for testing without going through Kafka (with a callable's
`agent_id`, it runs just that callable). Agent delegation is in-process
(ADR-0012): once the entry decision is final, the run graph fans out to
every callable agent whose `invoke_on` matches, in parallel (e.g.
`it-ops-triage`'s `root-cause-summarizer` on `ESCALATE`), folding their
`reasons[]` into the entry agent's before publishing `alert.decided`
(`docs/ARCHITECTURE.md` §3/§5). One level, one direction, scoped to one
app's own manifest — not cross-app A2A. Owns token-budget enforcement per
`app_id`/agent (an over-budget callable just doesn't contribute), a
timeout on every MCP call, and dedupe of at-least-once deliveries (a
`seen:{app_id}:{alert_id}` marker in Redis, set once the decision is
published). Retries and circuit breakers around tools live in
`tool-gateway`, not here.

### `tool-gateway`
An MCP server — the only service that executes tools. Every tool is a
read-only lookup (no remediation). Tools are either `global` (platform code
in `tool-gateway` itself, available to any app that allowlists them — e.g.
`similar-past-case-lookup`, which reads `review-console`'s `GET /cases`
for the run's own `app_id` + `alert_key`, taken from the run context,
never from arguments — ADR-0027) or `app`-scoped (owned by one app,
loaded at startup from `backend/apps/{app_id}/tools/`; fixture-backed in
this build). Re-checks every call against the calling agent's resolved
tools from `registry`, refuses a version other than the one it loaded
(ADR-0026), and validates the arguments against the schema registered in
`registry` and then the tool's own model before executing. Each call has
one deadline covering its retries with backoff, and each tool a circuit
breaker (ADR-0025), so a broken tool degrades predictably — the run
escalates naming it — instead of hanging the whole platform.

### `memory-store`
The only service that reads/writes behavioral history. Serves `GetContext`
over gRPC — rolling-window counts (1h/24h/7d) of decisions and analyst
verdicts per `alert_key`, plus `is_novel_alert`,
`has_confirmed_incident_history` and `has_confirmed_noise_history` (the
last two all-time, ADR-0031). History is an event log (ADR-0017): it
consumes `alert.decided` (ADR-0016) and `verdict.recorded` in one group,
storing one Postgres `memory_events` row per event — the source of truth.
Redis caches each key's last 7 days, prefixed by the app's
`memory_namespace` (looked up from `registry`, ADR-0018) so two apps'
identical alert keys never collide; a cache miss rebuilds from Postgres.
Verdicts feeding back into these counts is the platform's feedback loop
closing.

### `registry`
The capability and app directory. Two things live here: (1) agent/tool
registrations with versions and schemas, and (2) **App Manifests** — one
per configured use case, declaring that app's agents (incl. `invoke_on` for
callables), tools, event schema, and memory namespace. A manifest's source
of truth is `backend/apps/{app_id}/manifest.yaml`; `register_app.py`
upserts it here, and `registry` validates it on the way in. It serves each
app *resolved*: each tool reference is pinned or `latest` (the highest
registered version, resolved on every read, ADR-0026), and every agent
carries the tools it may call right now (allowlisted, declared, enabled).
The manifest also carries the limits others enforce: each agent's
`budget` (`orchestrator`) and the app's `rate_limit` (`ingestion`).
`ingestion`, `orchestrator` and
`tool-gateway` resolve "what am I allowed to do for this `app_id`" by
asking `registry` (cached 30s), never by hardcoding it; `memory-store` reads
only the app's `memory_namespace`. This is what makes adding app #2
or #3 a registration plus an app folder, not a platform code change.

### `review-console`
The human-in-the-loop surface. Consumes `alert.decided` and persists only
`ESCALATE` decisions into `cases` (auto-resolve/suppress are high-volume
and need no human, so they're never stored here; duplicates are ignored).
Exposes `GET /cases` (`app_id` required; filterable by `alert_key`,
`status`), `GET /cases/{id}` (the alert as it fired, the reasoning,
confidence, and every agent's tool calls — ADR-0023), and `POST
/cases/{id}/verdict` — `CONFIRMED_INCIDENT` / `CONFIRMED_NOISE`,
`verdict_by`, and optional `resolution_notes` (what actually fixed it) —
which moves the case `OPEN` → `RESOLVED` once and, in the same
transaction, writes a `verdict_outbox` row that the `outbox-relay`
publishes as `verdict.recorded` (ADR-0024). A Kafka outage doesn't refuse
a verdict; only Postgres being down does (`503`).
`resolution_notes` stays on the case; it reaches future agents via
`similar-past-case-lookup`.

### `outbox-relay` worker
One small asyncio process (`backend/workers/outbox-relay/`, ADR-0030), not
a platform service: no API beyond
`/healthz` and `/readyz` (role `active` or `standby`). It publishes pending `outbox` rows to
`alert.received`, or to `alert.received.dlq` after repeated
message-specific failures, and pending `verdict_outbox` rows to
`verdict.recorded` in the same loop (ADR-0024), each verdict timestamped
with the moment it was recorded. A Postgres session advisory lock keeps
exactly one instance draining, so one key's messages stay in order; a
second instance waits as `standby`.
It's the one background piece on the alert path: it adds up to one poll
interval (0.5s) of latency per alert when idle, in exchange for no alert
or verdict being lost during a Kafka outage (`ARCHITECTURE.md` §10). The
nightly batch re-evaluation (Day 25) isn't here: it's a script run by a
Kubernetes `CronJob`.

---

## Data plane

### Kafka
The event backbone connecting `ingestion` (via the `outbox-relay` worker),
`orchestrator`, `review-console`, and `memory-store` (which reads
`alert.decided` in its own consumer group). Four topics:
`alert.received`, `alert.decided` (both protobuf-encoded
`RunAgentRequest`/`Response` — the same contract used for the `orchestrator`
gRPC entrypoint, so there's only one schema to maintain),
`verdict.recorded`, and `alert.received.dlq` (alerts the relay gave up on,
for human inspection). Every message is keyed `{app_id}:{alert_key}`, so
one alert key's events stay ordered on one partition while different keys
spread across replicas (`ARCHITECTURE.md` §8). From Day 23, trace context
rides in Kafka headers so a single event's journey is one connected trace,
not five disjoint ones.

### Redis
`memory-store`'s hot-path cache of each alert key's recent events
(`mem:{memory_namespace}:{alert_key}:events`, a 7-day sorted set, plus
`:facts`), `orchestrator`'s
per-app/agent daily token counters (`budget:{app_id}:{agent_id}:{date}`)
dedupe markers (`seen:{app_id}:{alert_id}`),
`ingestion`'s per-minute rate-limit counters
(`ratelimit:{app_id}:{minute}`, `…:src:{source}:{minute}`). Budgets
and rate limits aren't enforced while Redis is down; the run or event
goes ahead.

### Postgres
Single local instance: `cases` (review-console, app-scoped),
`memory_events` (memory-store's event log, app-scoped), `apps` and `tools`
(registry's App Manifests and tool registrations), `outbox` (ingestion's
pending Kafka publishes) and `verdict_outbox` (review-console's), both
drained by the `outbox-relay` worker.

---

## Observability plane

### OTel Collector
Receives OTLP traces/logs/metrics from every service and fans out to the
three backends below. Before Day 23 of `docs/plan.md`, services log JSON to
stdout and create spans against a no-op-exported `TracerProvider`. The
Collector and its three backends run in Compose from Day 1 (it accepts OTLP
on `4317`/`4318`), but no service exports to it until Day 23.

### Tempo / Mimir / Loki
Traces, metrics, and logs respectively — each queried through Grafana, not
directly.

### Grafana
Dashboards: RED metrics per service, Kafka consumer lag, outbox backlog and
DLQ count, decision distribution (auto-resolve/escalate/suppress) and
latency per app, and — from Day 24 — LLM cost/token burn rate and tool-call
volume per app and agent. For the platform's operators only; it never
shows alert sources' or monitored systems' own telemetry.
