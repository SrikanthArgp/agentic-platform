# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An agentic microservices platform: event-triggered agents that decide,
explain themselves, and improve from human feedback. The platform core
(routing, tool-calling, memory, human review, observability) is generic;
domain-specific behavior is configuration (an "App Manifest"), not code.
The reference use case is IT-ops alert triage; three apps share the same
core in this build: `it-ops-triage`, `cost-anomaly-triage`,
`security-alert-triage`.

**Read `docs/ARCHITECTURE.md` before making structural changes** — it's the
system-of-record for the service map, transport choices, proto contracts,
and the multi-app model, with section numbers referenced directly from code
comments in `backend/proto/*.proto`. **Read `docs/adr/`** before changing or reversing a major decision — each
ADR records why it was made and what was rejected; a changed decision gets
a new ADR, not an edit. An ADR that no longer stands on its own (rejected
and never built, mostly superseded, or only a patch to another) is folded
into the one that covers it and deleted; numbers are never reused (rules in
`docs/adr/README.md`). Business scenarios (who would use each app, and where the platform
doesn't fit) are in `docs/SCENARIOS.md`. Security risks and required mitigations are in
`docs/ARCHITECTURE.md` §13; the phase-two path to an enterprise
deployment is `docs/ENTERPRISE_READINESS.md`. **Read `docs/plan.md`** for the
day-by-day build sequence and what each day's definition-of-done is — it's
the plan this repo is being built against, and should stay the source of
truth for "what order do we build things in."

## Current status (as of this writing)

Days 1–16 of `docs/plan.md` are done (infra, contracts, skeletons;
`tool-gateway`'s MCP server and first tool; `orchestrator`'s agent core;
`ingestion` and the end-to-end hot path; `registry` + App Manifest;
`memory-store`; context, supervisor and guardrails in `orchestrator`;
callable agents; `review-console`; the verdict feedback loop; the Week 2
integration pass; `tool-gateway` resilience; tool versions, the global
`similar-past-case-lookup` and app #2's first cut; token budgets and rate
limits; the outbox + the `outbox-relay` worker, for alerts and verdicts;
DLQ, dedupe and the Kafka-outage test); Day 17 (finish app #2 +
platform-core review) is next. As-built notes for each day are in `docs/plan.md`.

- All 6 services and the `outbox-relay` worker have a FastAPI app
  (`app/main.py`, `/healthz`), a `pyproject.toml`, a committed `uv.lock`,
  a `Dockerfile`, and passing tests.
- Nothing but `orchestrator` publishes to Kafka directly (Day 15,
  ADR-0030, ADR-0024). `ingestion` writes each accepted alert to Postgres
  `outbox`, and `review-console` writes each verdict to `verdict_outbox` in
  the transaction that resolves the case. The `outbox-relay` worker
  (`backend/workers/outbox-relay/`, host `:8007`, `/readyz` says `active`
  or `standby`) holds a Postgres session advisory lock and publishes
  `PENDING` rows in `id` order with their stored key and headers, verdicts
  timestamped by `created_at`; idle poll `RELAY_POLL_INTERVAL_S` (0.5s);
  `SENT` rows pruned after `OUTBOX_RETENTION_H` (24h). Kafka down → rows
  wait (no attempt counted), intake and verdicts still succeed. A message
  Kafka refuses (too large, corrupt, invalid topic; Day 16) counts an
  attempt and holds its table; after 5 (`RELAY_MAX_ATTEMPTS`) the row is
  `DEAD`, an alert row is copied to `alert.received.dlq` (headers
  `outbox_id`/`attempts`/`last_error`), and the relay moves on.
  `uv run backend/scripts/replay_dead.py` lists `DEAD` rows and
  (`--table T --id N`) sets one back to `PENDING`.
- Delivery is at-least-once, so consumers dedupe on `alert_id`:
  `orchestrator` skips an alert whose `seen:{app_id}:{alert_id}` Redis
  marker exists (set after its `alert.decided` is published, 24h
  `SEEN_TTL_S`; Redis down → it runs); `review-console` and `memory-store`
  rely on their unique keys. `uv run backend/scripts/burst.py send --out F`
  posts a burst through `ingestion`; `burst.py check F` verifies each alert
  was decided exactly once.
- `review-console` (`:8006`) consumes `alert.decided` (own group) and turns
  each `ESCALATE` into an `OPEN` row in `cases` (with `reasons`,
  `tool_calls`, `confidence`; unique `(app_id, alert_id)`). `GET /cases`
  (`app_id` required; `alert_key`, `status`, `limit`, `offset`),
  `GET /cases/{id}`, `POST /cases/{id}/verdict` (`CONFIRMED_INCIDENT` /
  `CONFIRMED_NOISE`, `verdict_by`, optional `resolution_notes`): `OPEN` →
  `RESOLVED` once, else `409`. A case also shows the alert itself and every
  agent's tool calls: `RunAgentResponse.alert` echoes the request and
  `ToolCall.agent_id` says whose call it was (ADR-0023). The verdict writes the `verdict.recorded`
  JSON (no notes) to `verdict_outbox` inside the row-lock transaction;
  only Postgres down → `503`, nothing committed.
- `memory-store` serves gRPC `GetContext` (`:50051`, host `:50053`): 1h/24h/7d
  decision and verdict counts plus `is_novel_alert`/
  `has_confirmed_incident_history` per `app_id` + `alert_key`. One consumer
  (group `memory-store`) reads `alert.decided` and `verdict.recorded` into
  Postgres `memory_events` (an event log, ADR-0017) and caches the last 7
  days per key in Redis (`mem:{memory_namespace}:{alert_key}:*`); a miss
  rebuilds from Postgres. `uv run backend/scripts/seed.py` seeds synthetic
  it-ops history; `uv run backend/scripts/get_context.py KEY [--repeat N]`
  queries it.
- `registry` (Postgres `tools`/`apps`, asyncpg) stores tool registrations
  and App Manifests, validates manifests on `PUT /apps/{app_id}`, and
  serves `GET /apps/{app_id}` resolved: each agent's `tools` = allowlisted,
  declared, enabled. A manifest tool `version` is pinned or `latest`
  (highest registered, resolved on every read, ADR-0026); a tool whose
  schemas aren't valid JSON Schema is rejected. `PATCH /tools/{id}/versions/{v} {"enabled": false}`
  turns a tool off for every agent within one TTL. Register an app with
  `uv run backend/scripts/register_app.py {app_id}` (needs the stack up;
  idempotent). `ingestion`, `orchestrator`, `tool-gateway` read it through
  `ap-shared`'s `registry_client` (30s TTL, `MANIFEST_TTL_S`, 404s cached).
- `ingestion`: `POST /apps/{app_id}/events` (envelope + `payload`)
  validates the payload against that app's `event_schema_ref`, builds
  `alert_key` from its `alert_key_fields`, writes the `alert.received`
  message to `outbox`, returns `202`; unknown app `404`; over the app's or source's per-minute
  rate limit `429` with `Retry-After` (manifest `rate_limit`, Redis,
  ADR-0029; Redis down → accepted). `202` means the `outbox` row is
  committed; Postgres down → `503`. It reads nothing back; decisions are
  seen on `review-console` (escalations) or the `alert.decided` topic.
- `tool-gateway` serves MCP (stateless Streamable HTTP) at `POST /mcp`. At
  startup it loads app tools from `backend/apps/*/tools/` (`app/core/loader.py`;
  the `TOOLS` dict contract, incl. required `read_only: True`, is in its
  docstring). Every call must carry a run context and be in that agent's
  `registry`-resolved tools, else `tool_not_allowed` (`registry_unavailable`
  if it can't check). Arguments must pass that tool's `input_schema` as
  registered, then its own model (`invalid_arguments`); a version other
  than the loaded one is `tool_version_mismatch` (ADR-0026). Global tools
  live in its own `app/tools/`: `similar-past-case-lookup` reads
  `review-console`'s resolved cases for the run's `app_id` + `alert_key`
  from the run context, never from arguments (`REVIEW_CONSOLE_URL`,
  ADR-0027). Each call has one
  deadline (`TOOL_TIMEOUT_S`, 5s) covering retries, and each tool a circuit
  breaker (`app/core/resilience.py`, ADR-0025). Other failures:
  `tool_not_found`/`tool_failed`/`tool_timeout`/`tool_unavailable`
  (breaker open). `TOOL_FAULTS=lookup_runbook=error` (or `=sleep:30`)
  breaks a tool on purpose for live tests. `uv run backend/scripts/mcp_call.py [tool_id] [json-args]`
  calls it from the host (`localhost:8003`) as `--app-id`/`--agent-id`
  (default it-ops-triage/triage-agent; the app must be registered).
- `orchestrator` consumes `alert.received`, runs the app's agents, and
  publishes `alert.decided`. The run is a LangGraph graph
  (`app/agent/graph.py`, ADR-0022) and each agent's tool loop is
  LangChain's `create_agent` with `ChatOpenAI` (ADR-0022, incl. the model);
  tools are our own `StructuredTool`s over MCP (`app/agent/react.py`); the same run is exposed as gRPC `RunAgent`
  (`:50051`, host `:50052`). Before the loop it calls `memory-store`'s
  `GetContext` once (one shared channel, `MEMORY_STORE_TARGET`) and gives
  the agent the history as a data block (ADR-0019), then calls the
  agent's manifest `prefetch_tools` with no arguments and adds their
  results the same way (ADR-0028; both apps prefetch
  `similar-past-case-lookup`). Each agent has a daily token budget
  (manifest `budget.daily_tokens`, else `DAILY_TOKEN_BUDGET`; Redis
  `budget:{app_id}:{agent_id}:{date}`, ADR-0029): an entry agent over it
  isn't run ("not evaluated: budget exceeded"), a callable over it doesn't
  contribute. After it, in order:
  the supervisor (agent's `confidence`, capped by fixed rules, below the
  manifest's `supervisor.min_confidence` / default 0.6 → `ESCALATE`,
  ADR-0020), then the manifest's `escalate_when` guardrails (`alert.*`,
  `payload.*`, `context.*`; a match → `ESCALATE`, named in `reasons[]`,
  ADR-0010). Both only ever move toward `ESCALATE`; memory down →
  the run continues without context and the supervisor escalates. A run
  that can't happen (unknown app, `registry` down with no cached copy,
  `tool-gateway` or the LLM unreachable) publishes `ESCALATE` "not
  evaluated" instead.
  `RunAgentResponse.confidence` carries the final value. Then every
  callable agent whose `invoke_on` has the final decision runs in parallel,
  in-process (ADR-0012), and its reasons are appended prefixed with its
  `agent_id`; a failing or slow one (`CALLABLE_TIMEOUT_S`) only adds a
  "didn't contribute" reason. `RunAgent` with a callable's `agent_id` runs
  just that callable. It resolves the app from `registry` and offers
  the agent exactly its resolved `tools`; prompts are read from
  `backend/apps/{app_id}/prompts/` in its image. Needs
  `OPENAI_API_KEY` in `backend/local/.env` (see `.env.example`).
  `uv run backend/scripts/publish_alert.py` publishes straight to Kafka
  (bypassing `ingestion`) and prints the decision. Unit tests use a scripted LangChain fake chat model; none call the
  real one.
- Run context (`app_id`/`agent_id`/`alert_id`/`alert_key`) travels in MCP
  `_meta` via `ap-shared`'s `run_context` module, never as a tool argument.
- `backend/local/docker-compose.yml` runs the full stack: Kafka (KRaft),
  Redis, Postgres, OTel Collector, Loki, Mimir, Tempo, Grafana, the 6
  services and the `outbox-relay` worker — all with health checks. The one-shot `kafka-init` service
  creates the four topics with 12 partitions (broker auto-create is off).
  `backend/local/postgres/init.sql` creates `cases`, `memory_events`,
  `tools`, `apps`, `outbox`, `verdict_outbox`; it's idempotent, so on an existing volume pipe it into
  `docker compose exec -T postgres psql -U platform -d platform`.
  After `down -v`, re-run `register_app.py` for both apps.
- Services log JSON to stdout and drop spans; nothing exports to the
  collector until Day 23. Don't set `OTEL_EXPORTER_OTLP_ENDPOINT` on a
  service before then — FastAPI >=0.142 auto-attaches OTLP exporters when
  it's set.
- Generated proto stubs in `backend/shared/proto_gen/` are committed; rerun
  `backend/scripts/gen_proto.sh` after editing any `.proto`.
- `backend/apps/it-ops-triage/` has `tools/` (`lookup_runbook` +
  `runbooks.json`; `recent-changes-lookup` + `recent_changes.json`, with
  absolute timestamps around 2026-10-05, so set an alert's `timestamp` near
  one to see a change cited), a complete `manifest.yaml` (guardrail `alert.severity in
  [critical]`, `supervisor.min_confidence: 0.6`), `event_schema.json`, `prompts/triage-agent.md` and `prompts/root-cause-summarizer.md` (callable, `invoke_on: [ESCALATE]`).
- `backend/apps/cost-anomaly-triage/` (app #2, first cut; finished Day 17):
  `cost-triage-agent` with `billing-lookup` (`tools/billing.json`, daily
  line items around 2026-10-01..08 for `checkout-svc`/`us-east-1`,
  `ml-platform`/`eu-west-1`, `search-api`/`us-west-2`) and the global
  `similar-past-case-lookup`; `alert_key` is `service:region`.

## How each day's work is done

Every `docs/plan.md` day follows the same routine, tracked as a task list:

1. **Start with a checklist.** Read the day in `docs/plan.md` (and the ADRs
   it cites), then post a task list grouped as: reading, schema/infra, each
   service or worker touched, tests, docs, live check, commit.
2. **Show progress as you go.** Repost the list with done items checked
   (`- [x]`) at each milestone and whenever asked. Only check an item once it
   is actually done and verified (tests run, live check observed); a step
   that was skipped or failed stays unchecked, with the reason.
3. **Tests**: `uv run pytest` in every project touched (and the rest, as a
   regression pass), then `uv run pytest -m integration` with the stack up.
4. **Live check**: run the day's definition of done against the Compose
   stack and record what was observed (numbers, ids) in the day's
   **As built** notes in `docs/plan.md`.
5. **Docs sweep**: update `docs/ARCHITECTURE.md`, `docs/BLOCK_DIAGRAM.md`,
   `docs/sequence_diagram.md`, each touched `CLASS_DIAGRAM.md`, this file's
   status section, and mark the day ✅ in `docs/plan.md`. Then grep for
   wording that still describes the day as future work ("from Day N",
   "until Day N", "on Day N … will") outside `plan.md`, and fix it.
6. **Commit** on a `day-N` branch, only when the user asks.

## Commands

There is no root workspace file — every service under `backend/services/*`
and `backend/shared` is its own independent `uv` project, path-depending on
`ap-shared` (`backend/shared`) via `[tool.uv.sources]`. Run commands from
inside each service's directory:

```bash
cd backend/services/<service>   # ingestion | orchestrator | tool-gateway | memory-store | registry | review-console
uv sync                          # install deps (incl. editable ap-shared)
uv run pytest                    # run that service's tests
uv run pytest tests/test_main.py::test_healthz_returns_200_with_expected_shape  # single test
uv run uvicorn app.main:app --reload --port 8000   # run locally
uv run pytest -m integration     # integration tests (orchestrator, ingestion, registry, memory-store, review-console): need the Compose stack up
```

The `outbox-relay` worker is another independent `uv` project, tested the
same way (`uv run pytest -m integration` runs its SQL against the Compose
Postgres in a throwaway schema):

```bash
cd backend/workers/outbox-relay
uv sync
uv run pytest
```

`backend/shared` (the `ap-shared` package: proto stubs + `observability`
module) has its own tests:

```bash
cd backend/shared
uv sync
uv run pytest
```

Regenerate gRPC stubs after editing any `.proto` file (writes into
`backend/shared/proto_gen/`, consumed by every service via `ap-shared`):

```bash
backend/scripts/gen_proto.sh
```

Python version is pinned to 3.12 (`backend/.python-version`).

Platform-wide check (from `backend/local`):

```bash
docker compose up -d --build --wait   # all containers healthy
# services on host ports 8001–8006 (ingestion, orchestrator, tool-gateway,
# memory-store, registry, review-console), outbox-relay on 8007; Grafana on :3000 (anonymous admin);
# Postgres :5432 (platform/platform), Redis :6379, Kafka localhost:29092
docker compose down -v                # reset volumes (re-runs init.sql)
```

then `pytest` green in every service directory and in `backend/workers/outbox-relay`.

## Architecture essentials

(Full detail in `docs/ARCHITECTURE.md` — this is just enough to navigate.)

- **Six services**: `ingestion` (REST intake), `orchestrator` (agent loop),
  `tool-gateway` (MCP tool server), `memory-store` (gRPC context store),
  `registry` (REST capability/app discovery), `review-console` (REST
  human-in-the-loop), plus one background worker, `outbox-relay` (Day 15,
  ADR-0030), that drains the `outbox` and `verdict_outbox` tables to Kafka.
- **Transport is protocol-per-purpose, not uniform**: Kafka between
  `ingestion`/`orchestrator`/`review-console`/`memory-store` (the event
  backbone: `alert.received`, `alert.decided`, `verdict.recorded`), gRPC for
  `orchestrator → memory-store` (`GetContext`, synchronous), MCP for
  `orchestrator → tool-gateway` (tool-calling), REST for `registry` reads
  and both human-facing APIs (`ingestion`, `review-console`).
  Agent-to-agent (A2A) is explicitly not part of this design.
- **One proto contract, two transports**: `RunAgentRequest`/`Response` in
  `agent.proto` is both the `orchestrator` gRPC message shape *and* the
  `alert.received`/`alert.decided` Kafka payload (protobuf-encoded) — there
  is deliberately no second schema to keep in sync.
- **Multi-app model**: `registry` stores an "App Manifest" per app (agents,
  tools, event schema ref, memory namespace) — see `docs/ARCHITECTURE.md`
  §3 for the exact shape. Every app-scoped request/row carries `app_id`;
  nothing infers which app it belongs to. App-specific code (prompt, event
  schema, app-owned tools) is meant to live under `backend/apps/{app_id}/`,
  never inside a platform service.
- **`ap-shared`** (`backend/shared`) is the one cross-service dependency:
  generated proto stubs (`proto_gen/`) and the `observability` module
  (`setup_observability()` — stdout JSON logging + a no-op-exported
  `TracerProvider` for now; Week 5 (Day 23) of `docs/plan.md` points it at a real
  OTel Collector without changing that API).
- **Explainability is a field, not a log line**: agent decisions carry
  `reasons[]`, populated from the actual tool-call trace — this is what
  `review-console` shows an analyst, and it's a hard contract, not
  incidental.
- **Triage only — no detection, no remediation** (`docs/ARCHITECTURE.md`
  §2, §11): alerts come from external systems (monitoring, cost-anomaly
  detectors, SIEMs) that live outside the platform; agents decide
  `AUTO_RESOLVE`/`ESCALATE`/`SUPPRESS`. Every tool is a read-only lookup —
  never add a tool that changes external state. App-owned tools are
  fixture-backed in this build.
- **An app is config + a folder, not a service** (§3, §12): a tenant is an
  app (no per-customer tenancy). One `orchestrator` runs every app's
  agents; routing is by the `POST /apps/{app_id}/events` URL, never
  inferred. Each app has exactly one `entry` agent plus any number of
  `callable` agents, run by `orchestrator` when their manifest `invoke_on`
  matches the final decision — one level deep, never LLM-chosen. A
  manifest's source of truth is `backend/apps/{app_id}/manifest.yaml`,
  registered via `backend/scripts/register_app.py`.
- **Prompt inputs are data, never instructions** (§5, §13): alert payloads
  (attacker-influenced, especially in `security-alert-triage`), tool
  results, and `resolution_notes` go into prompts as delimited data.
  Deterministic manifest `escalate_when` guardrails force `ESCALATE` after
  the LLM and can never be lowered by it; `app_id` reaches tools via
  request context, never as a tool argument.
- **Deployment** (§12, ADR-0014): one container per platform service, never per app;
  `ingestion`/`orchestrator`/`tool-gateway` images `COPY` `backend/apps/`.
  Compose for apps #1–#2 and the dev loop; from Day 18 a local kind
  cluster (`backend/deploy/k8s/`, Kustomize) where app #3 onward ships by
  rolling update (`rollout_app.sh`: roll the three services, wait, then
  `register_app.py`).
  The `outbox-relay` worker (`backend/workers/outbox-relay/`, one active
  instance via a Postgres advisory lock, ADR-0030) relays both outbox
  tables; there is no Celery. The Day 25 batch eval is a script,
  run nightly by a Kubernetes `CronJob`.
