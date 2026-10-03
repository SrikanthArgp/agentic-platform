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
a new ADR, not an edit. Business scenarios (who would use each app, and where the platform
doesn't fit) are in `docs/SCENARIOS.md`. Security risks and required mitigations are in
`docs/ARCHITECTURE.md` §13; the phase-two path to an enterprise
deployment is `docs/ENTERPRISE_READINESS.md`. **Read `docs/plan.md`** for the
day-by-day build sequence and what each day's definition-of-done is — it's
the plan this repo is being built against, and should stay the source of
truth for "what order do we build things in."

## Current status (as of this writing)

Days 1–4 of `docs/plan.md` are done (infra, contracts, skeletons;
`tool-gateway`'s MCP server and first tool; `orchestrator`'s agent core;
`ingestion` and the end-to-end hot path); Day 5 (`registry` + App
Manifest) is next. As-built notes for each
day are in `docs/plan.md`.

- All 6 services have a FastAPI skeleton (`app/main.py`, `/healthz`),
  a `pyproject.toml`, a committed `uv.lock`, a `Dockerfile`, and passing
  health-check tests. `memory-store`, `registry`, and `review-console`
  package subdirectories are still empty `__init__.py` stubs.
- `ingestion`: `POST /alerts` (envelope + `payload`) validates the payload
  against the app's `event_schema.json`, builds `alert_key` from the
  manifest's `alert_key_fields`, publishes `alert.received`, returns `202`.
  `app_id` is fixed to `it-ops-triage` until Day 5. `GET /alerts/{id}` is a
  throwaway debug view of the decision (until Day 9's `review-console`).
- `tool-gateway` serves MCP (stateless Streamable HTTP) at `POST /mcp`. At
  startup it loads app tools from `backend/apps/*/tools/` (`app/core/loader.py`;
  the `TOOLS` dict contract is in its docstring). Tool failures are
  `is_error` results with a `tool_not_found`/`invalid_arguments`/`tool_failed`
  code. `uv run backend/scripts/mcp_call.py [tool_id] [json-args]` calls it
  from the host (`localhost:8003`).
- `orchestrator` consumes `alert.received`, runs the entry agent's
  tool-calling loop (OpenAI, ADR-0015, behind `app/agent/llm.py`), and
  publishes `alert.decided`; the same run is exposed as gRPC `RunAgent`
  (`:50051`, host `:50052`). It reads the app manifest from
  `backend/apps/{app_id}/manifest.yaml` until Day 5's `registry`. Needs
  `OPENAI_API_KEY` in `backend/local/.env` (see `.env.example`).
  `uv run backend/scripts/publish_alert.py` publishes straight to Kafka
  (bypassing `ingestion`) and prints the decision. Unit tests use a scripted fake LLM; none call the
  real one.
- Run context (`app_id`/`agent_id`/`alert_id`) travels in MCP `_meta` via
  `ap-shared`'s `run_context` module, never as a tool argument.
- `backend/local/docker-compose.yml` runs the full stack: Kafka (KRaft),
  Redis, Postgres, OTel Collector, Loki, Mimir, Tempo, Grafana, and the 6
  services — all with health checks. The one-shot `kafka-init` service
  creates the four topics with 12 partitions (broker auto-create is off).
  `backend/local/postgres/init.sql` creates `cases` and `memory_history`.
- Services log JSON to stdout and drop spans; nothing exports to the
  collector until Day 23. Don't set `OTEL_EXPORTER_OTLP_ENDPOINT` on a
  service before then — FastAPI >=0.142 auto-attaches OTLP exporters when
  it's set.
- Generated proto stubs in `backend/shared/proto_gen/` are committed; rerun
  `backend/scripts/gen_proto.sh` after editing any `.proto`.
- `backend/apps/it-ops-triage/` has `tools/` (`lookup_runbook` +
  `runbooks.json`), a partial `manifest.yaml` (agents, tools,
  `event_schema_ref`, `alert_key_fields`), `event_schema.json`, and
  `prompts/triage-agent.md`.
- `backend/scripts/seed.py` is a stub docstring, no implementation.

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
uv run pytest -m integration     # integration tests (orchestrator, ingestion): need the Compose stack up
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
# memory-store, registry, review-console); Grafana on :3000 (anonymous admin);
# Postgres :5432 (platform/platform), Redis :6379, Kafka localhost:29092
docker compose down -v                # reset volumes (re-runs init.sql)
```

then `pytest` green in every service directory.

## Architecture essentials

(Full detail in `docs/ARCHITECTURE.md` — this is just enough to navigate.)

- **Six services**: `ingestion` (REST intake), `orchestrator` (agent loop),
  `tool-gateway` (MCP tool server), `memory-store` (gRPC context store),
  `registry` (REST capability/app discovery), `review-console` (REST
  human-in-the-loop), plus Celery workers for batch/background work.
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
  Celery (Redis broker, `celery-beat`) runs the Day 15 outbox relay and
  the Day 25 batch eval.
