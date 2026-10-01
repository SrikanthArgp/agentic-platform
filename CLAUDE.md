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
a new ADR, not an edit. Security risks and required mitigations are in
`docs/ARCHITECTURE.md` §13; the phase-two path to an enterprise
deployment is `docs/ENTERPRISE_READINESS.md`. **Read `docs/plan.md`** for the
day-by-day build sequence and what each day's definition-of-done is — it's
the plan this repo is being built against, and should stay the source of
truth for "what order do we build things in."

## Current status (as of this writing)

Pre-implementation, mid-way through Day 1 of `docs/plan.md`. Nothing is
runnable end-to-end yet:

- `ingestion`, `orchestrator`, `memory-store`, `registry`, `review-console`
  each have a FastAPI skeleton (`app/main.py`, `/healthz` only), a
  `pyproject.toml`, a `Dockerfile`, and one passing health-check test.
  Package subdirectories (`app/api`, `app/kafka`, `app/grpc`, etc.) exist
  as empty `__init__.py` stubs, matching the layout described below, but
  contain no logic yet.
- `tool-gateway` is further behind: only empty `__init__.py` stubs under
  `app/core`, `app/grpc`, `app/mcp` — no `main.py`, `pyproject.toml`, or
  `Dockerfile` yet.
- `backend/proto/agent.proto` and `memory_store.proto` are written
  (including the `app_id` field the multi-app model requires), but
  `backend/scripts/gen_proto.sh` has not been run — `backend/shared/proto_gen/`
  is still an empty package, and `backend/shared/tests/test_proto_gen.py`
  self-skips until it has been.
- No `backend/local/docker-compose.yml` and no
  `backend/local/postgres/init.sql` yet — both are Day 1 scope in
  `docs/plan.md` and don't exist. `docker compose up` is not yet possible.
- `backend/scripts/seed.py` is a stub docstring, no implementation.
- No `backend/apps/` directory yet (where app-specific adapters — event
  schema, prompt, app-owned tools — are meant to live per
  `docs/ARCHITECTURE.md` §3).

## Commands

There is no root workspace file — every service under `backend/services/*`
and `backend/shared` is its own independent `uv` project, path-depending on
`ap-shared` (`backend/shared`) via `[tool.uv.sources]`. Run commands from
inside each service's directory:

```bash
cd backend/services/<service>   # ingestion | orchestrator | memory-store | registry | review-console
uv sync                          # install deps (incl. editable ap-shared)
uv run pytest                    # run that service's tests
uv run pytest tests/test_main.py::test_healthz_returns_200_with_expected_shape  # single test
uv run uvicorn app.main:app --reload --port 8000   # run locally
```

`tool-gateway` has no `pyproject.toml` yet, so none of the above works there
until its Day 1 scaffolding lands (`docs/plan.md` Day 1).

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

Once `docker-compose.yml` exists (Day 1), the platform-wide check is:
`docker compose up` → all services `200` on `/healthz` → `pytest` green in
every service directory.

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
  `TracerProvider` for now; Week 5 (Day 21) of `docs/plan.md` points it at a real
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
- **Deployment** (§12): one container per platform service, never per app;
  `ingestion`/`orchestrator`/`tool-gateway` images `COPY` `backend/apps/`.
  Celery (Redis broker, `celery-beat`) runs the Day 15 outbox relay and
  the Day 23 batch eval.
