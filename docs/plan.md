# Build Plan: 27-Day Sequence

Status: Days 1–7 done; Day 8 is next. Implements the design in `docs/ARCHITECTURE.md`. Each day builds on a *runnable* system from the day before — nothing is "wire it all up at the end."

Service map this plan assumes (see `docs/ARCHITECTURE.md` for full rationale): `ingestion`, `orchestrator`, `memory-store`, `review-console`, `registry`, `tool-gateway`, plus Celery workers. The platform is multi-app: three configurable use cases (products) run on the same shared core, each registered as an **App Manifest** in `registry` (introduced Day 5) rather than hardcoded. App #1 / reference domain adapter: IT ops alert triage (`it-ops-triage`; `ingestion` accepts alerts; `orchestrator`'s agent decides auto-resolve / escalate / suppress-as-noise). App #2 is **cloud cost-anomaly triage** (`cost-anomaly-triage`): a spend-spike alert from a cloud billing/cost tool. App #3 is **security alert triage** (`security-alert-triage`): SIEM/EDR alerts. All three use the same `AUTO_RESOLVE` / `ESCALATE` / `SUPPRESS` decision enum from `agent.proto` — apps that need a different decision vocabulary would require a proto change and are out of scope. App #2 gets a first cut on Day 13 and is finished Day 17; a local Kubernetes cluster is stood up on Days 18–19 (ADR-0014), and app #3 is built Day 20 and shipped to it by rolling update. Agent-to-agent (A2A) communication between apps is explicitly out of scope — apps are isolated tenants sharing infrastructure (Kafka/gRPC/REST/MCP transports), not a mesh. A "tenant" is an app; there is no per-customer tenancy in this build.

Scope boundary for every day (`docs/ARCHITECTURE.md` §2, §11): the platform **triages** alerts that external systems already raised — it doesn't detect anomalies and doesn't remediate. Every tool is a read-only lookup backed by a fixture in this build; real data connectors are phase two (`docs/ENTERPRISE_READINESS.md`).

**Shape of the build**

| Week | Days | Theme | Ends with |
|---|---|---|---|
| 1 | 1–5 | Agent fundamentals | One app, end to end, discovered via its manifest |
| 2 | 6–10 | Context, guardrails, delegation, human feedback | Decisions shaped by memory, bounded by guardrails, reviewed by humans, improved by verdicts |
| 3 | 11–15 | Integration + platform hardening | Resilient tools, app #2 first cut, budgets/rate limits, outbox relay |
| 4 | 16–20 | Reliability, Kubernetes, app #3 | Exactly-once decisions under Kafka failure; app #2 finished; local Kubernetes ready; app #3 rolled out with zero interruption to apps #1–#2 |
| 5 | 21–25 | Three apps concurrently, observability, evals | Simulator, three apps side by side, traces, cost dashboards, eval gate |
| 6 | 26–27 | Load + polish | Load numbers, README |

Buffer is built in on purpose: Day 11 and Day 22 are integration days with slack for whatever overran that week, and Day 27 morning is unscheduled. Don't fill them with new scope.

## Cross-cutting rules for every day

- **Instrument as you build, not at the end.** Each service gets OpenTelemetry SDK + structured logging the day it's created, even before the collector/backends exist (logs to stdout, traces no-op'd). Day 23 wires the *pipeline*, not six services' instrumentation at once.
- **Every day ends with something running in Docker Compose.** If a day's work isn't runnable via `docker compose up`, it isn't done. From Day 18 it must *also* deploy to the local kind cluster (`backend/deploy/k8s/`, ADR-0014); Compose stays the dev loop, Kubernetes is where apps are rolled out.
- **Proto contracts are written before the services that implement them** — they're the interface, not an afterthought.
- **Unit tests are part of each day's work, not a separate pass.** Every day ends with a "Unit tests" step for the logic written that day; that day isn't done until those tests pass locally (`pytest`). Integration-style plumbing (gRPC/Kafka wiring) is covered by each day's "Definition of done" check — unit tests target the pure logic underneath (routing, scoring/threshold math, window aggregates, validation), which is what's easy to get subtly wrong and hard to catch by eyeballing a demo.
- **Commit at the end of each day** with the system in a working state, so any day can be a checkpoint to roll back to.
- **Platform core vs. domain adapter stays a live distinction, not just a doc section.** Anything specific to IT-ops (alert schema, runbook-lookup tool, the triage prompt) lives in a clearly separate module from platform code (registry, tool-gateway, orchestrator's agent-loop skeleton, memory-store's generic context API) — the whole point of this project is that a second (and third) domain can plug in without touching the platform core.
- **App identity is explicit, everywhere.** Every event envelope, gRPC request, and persisted row that's app-specific carries an `app_id`. Nothing about routing to the right agent, tool set, or memory namespace is inferred — it's looked up from that app's App Manifest in `registry`.
- **A changed decision gets an ADR.** If a day's work reverses or changes something in `docs/adr/`, write the new ADR that day, not later.

---

## Week 1 — Agent fundamentals

### Day 1 — Foundation: infra, contracts, skeletons ✅

**Goal**: `docker compose up` brings up every piece of infrastructure the platform needs, and all 6 services register as empty-but-healthy.

- Repo scaffold: `backend/services/{ingestion,orchestrator,memory-store,review-console,registry,tool-gateway}`, `backend/proto/`, `backend/local/`, `docs/`.
- `backend/local/docker-compose.yml`: Kafka (KRaft mode), Redis, **local Postgres** (not hosted — simpler for solo dev), OTel Collector, Loki, Mimir, Tempo, Grafana — all with health checks.
- `backend/local/postgres/init.sql`: minimal `cases` and `memory_history` schemas.
- Proto contracts: `memory_store.proto` (`GetContext`), `agent.proto` (`RunAgent`) in `backend/proto/` — already written, including `app_id`, `payload`, and the verdict counts. Run `backend/scripts/gen_proto.sh` to generate Python stubs into `backend/shared/proto_gen/`, imported by both `orchestrator` and `memory-store`. The stubs are committed (images `COPY shared` as-is); rerun the script after any `.proto` edit.
- Generate and commit a `uv.lock` for every service and `backend/shared` — every Dockerfile runs `uv sync --frozen`, which fails without one.
- Each of the 6 FastAPI services: skeleton app, `/healthz`, Dockerfile, OTel SDK wired to log to stdout (collector not yet consuming).
- Deployment layout per `docs/ARCHITECTURE.md` §12: one container per service, never per app. Create `backend/apps/it-ops-triage/` (empty placeholder is fine) so the `ingestion`, `orchestrator`, and `tool-gateway` Dockerfiles can `COPY apps apps` from the `backend/` build context starting today; the other three services don't copy it. Compose bind-mounts `backend/apps` into those three for local iteration.
- **Unit tests**: request/response schema validation for each service's skeleton endpoints; the existing `backend/shared` test that generated proto stubs import cleanly and match `.proto` field names (no longer skipped once stubs exist).

**Definition of done**: `docker compose up` → all containers healthy, all 6 services respond `200` on `/healthz`, Grafana loads (empty dashboards okay), `pytest` passes across all service packages.

**As built** (notes for later days):
- Host ports: services `8001`–`8006` (ingestion, orchestrator, tool-gateway, memory-store, registry, review-console), Grafana `3000` (anonymous admin), Postgres `5432` (`platform`/`platform`), Redis `6379`, Kafka `localhost:29092` from the host / `kafka:9092` in Compose, Collector OTLP `4317` (gRPC) / `4318` (HTTP).
- The upstream OTel Collector and Mimir images are distroless, so `backend/local/{otel-collector,mimir}/Dockerfile` re-home their binaries onto busybox to get `wget` for health checks.
- Service Dockerfiles set `UV_NO_SYNC=1`; without it `uv run` re-syncs at container start and installs the dev group that `uv sync --no-dev` left out.
- Services get no `OTEL_EXPORTER_OTLP_ENDPOINT` yet: FastAPI ≥0.142 auto-attaches OTLP exporters whenever it's set (see Day 23).

---

### Day 2 — `tool-gateway`: first real MCP tool ✅

**Goal**: a working MCP server exposing one real tool, callable by anything that speaks MCP.

- Implement an MCP server in `tool-gateway` exposing `lookup_runbook(alert_type) -> RunbookEntry`.
- Seed static/in-memory runbook data (JSON fixture) — no external dependency yet.
- Tool schema (input/output) defined explicitly, not inferred — this schema is what `registry` will later store.
- `lookup_runbook` is IT-ops-specific, so it lives in `backend/apps/it-ops-triage/tools/`, not in `tool-gateway`'s own code. `tool-gateway` loads it via the startup scan described in `docs/ARCHITECTURE.md` §12 (import each `backend/apps/*/tools/` module by file path, register its tools by `tool_id`) — build that loader now rather than hardcoding the tool, so apps #2/#3 plug in the same way.
- **Unit tests**: tool input schema validation, lookup logic (found / not-found cases), the loader (discovers tools from a fixture apps dir; an unknown `tool_id` returns an explicit not-found).

**Definition of done**: a standalone MCP test client (or simple script) calls `tool-gateway` and gets a real runbook entry back for a seeded alert type, and a clear "not found" for an unseeded one.

**As built** (notes for later days):
- MCP SDK `mcp` 2.x (`>=2.2,<3`), low-level `Server`, so tools are registered from the loader with their declared schemas, never inferred from function signatures. Served as stateless Streamable HTTP with JSON responses at `POST /mcp` on the service port (`http://tool-gateway:8000/mcp` in Compose, `http://localhost:8003/mcp` from the host). Stateless means any replica serves any call.
- App tool contract: each non-`_` module in `backend/apps/{app_id}/tools/` exports `TOOLS = {tool_id: {version, description, input_model, output_model, handler}}` (pydantic models, sync or async handler). App modules import no platform code. Any load problem (import error, bad `TOOLS`, duplicate `tool_id`) fails startup, naming the file.
- Errors are tool results, not protocol errors: `is_error=True` with `structured_content.error` set to `tool_not_found`, `invalid_arguments`, or `tool_failed` (internals not leaked). A runbook that doesn't exist is a normal result (`found: false`), not an error. Day 3's MCP client should branch on `is_error` and these codes.
- `tools/list` carries each tool's `version`, `scope`, `app_id` in `_meta` and `read_only_hint: true`. These are the fields Day 4's `registry` will want.
- DNS-rebinding protection on `/mcp`: only Host headers in `MCP_ALLOWED_HOSTS` (default `localhost:*,127.0.0.1:*,tool-gateway:*`) are served; others get `421`. The kind cluster (Day 18) must add its Service DNS names.
- `APPS_DIR` overrides where tools are scanned from (default `backend/apps`, same layout in the image).
- `backend/scripts/mcp_call.py` is the standalone client (PEP 723 script, `uv run backend/scripts/mcp_call.py [tool_id] [json-args]`).
- No allowlist or `app_id` request context yet; Days 3 and 13 (§13 T4).

---

### Day 3 — `orchestrator`: agent core with tool-calling ✅

**Goal**: an agent that receives an alert, calls a real tool via `tool-gateway`, and produces a decision with reasons.

- **Decide the LLM provider and model** for this build (and record it — an ADR if it's a real choice between providers).
- LLM client integration (tool-use / function-calling format) inside `orchestrator`, behind a small provider-agnostic interface (messages, tool definitions, tool results, token usage) so the agent loop never calls a provider SDK directly — near-free now, and the basis for per-agent `model_ref` later (`docs/ENTERPRISE_READINESS.md` §3).
- `RunAgent(Alert) -> AgentDecision` gRPC handler: builds tool definitions from `tool-gateway`, runs the tool-calling loop, returns `{decision, reasons[]}` — `reasons[]` is the explainability contract, same role as Sentinel's rule-names field, now populated by the agent's tool-call trace.
- Kafka consumer on `alert.received` (no producer yet — test by hand-publishing).
- Prompt-injection baseline (`docs/ARCHITECTURE.md` §5, §13 T1/T2): the alert payload and every tool result go into the prompt as delimited, labelled data blocks; the system prompt states nothing inside them is an instruction. The run's `app_id`/`agent_id` travel in the MCP request context set by `orchestrator`, never as tool arguments — tool schemas don't expose them.
- **Unit tests**: decision-parsing logic, tool-call routing against a **mocked** `tool-gateway` client (no live LLM or network calls in unit tests — use fixture responses), prompt assembly wraps payload/tool results in data blocks (including payload text that contains delimiter-like strings), tool calls carry `app_id` in context and never in arguments.

**Definition of done**: hand-publish an `alert.received` event, observe `orchestrator` consume it, call `tool-gateway` for real, and produce an `alert.decided` event with a populated `reasons[]`.

**As built** (notes for later days):
- LLM: OpenAI, default `gpt-5.4-mini`, set by `LLM_MODEL` (ADR-0015). `app/agent/llm.py` is the provider-agnostic interface; `app/agent/openai_llm.py` is the only module importing `openai`. The key is `OPENAI_API_KEY` in the gitignored `backend/local/.env` (template: `.env.example`), passed by Compose to `orchestrator` only.
- Interim manifest: `backend/apps/it-ops-triage/manifest.yaml` exists with `agents` and `tools` only, read from disk by `app/core/manifest.py` (`FileManifestStore`). Day 5 swaps that class for a `registry` client and adds the remaining fields; the `AppManifest` model ignores unknown fields. Prompt: `prompts/triage-agent.md` (the app part); the platform part (data-block rules, JSON answer format) is `PLATFORM_RULES` in `app/agent/prompt.py`.
- Tools offered to the LLM: on the agent's `tool_allowlist` *and* global or owned by the run's app (`visible_tools`). A tool call outside that set is refused in `orchestrator` (`tool_not_allowed`) and never reaches `tool-gateway`.
- Data blocks: `<<<DATA {nonce} kind=...>>>` … `<<<END DATA {nonce}>>>`, a random nonce per run, JSON-encoded content. The alert envelope *and* payload are inside the block, not just the payload.
- Fail toward `ESCALATE`: an unparseable final answer, hitting `MAX_TOOL_ROUNDS` (default 5), or an infrastructure tool failure (`tool_failed`, `gateway_unreachable`) escalate with an `orchestrator: ` reason. A run that can't happen at all (LLM down, unknown app) still publishes `alert.decided` as `ESCALATE` with a `not evaluated: …` reason. Day 7's supervisor/guardrails add to this; they don't replace it.
- Run context: the shared `run_context` module (`ap-shared`) puts `app_id`/`agent_id`/`alert_id` in MCP `_meta` under `agentic-platform/*` keys. `tool-gateway` reads and logs it; enforcement is Day 13.
- Kafka: `app/kafka/alerts.py` consumes `alert.received` (group `orchestrator`, manual commit after publishing) and produces `alert.decided` keyed `{app_id}:{alert_key}` with an idempotent producer; it reconnects if Kafka is down. Topics are still auto-created with 1 partition, so `orchestrator` logs a few `Topic … not found` errors at first start; Day 4 creates them explicitly.
- gRPC: `Agent.RunAgent` on port `50051` (host `50052`). Unknown app/agent → `NOT_FOUND`; other failures → `ESCALATE` "not evaluated", same as Kafka.
- `backend/scripts/publish_alert.py` hand-publishes `alert.received` and waits for the matching `alert.decided` (stand-in for `ingestion` until Day 4).
- First latency read (live, `gpt-5.4-mini`, one tool call): 3–5 s per decision, about 2.3k input / 150 output tokens.
- Not yet: memory context and the supervisor/guardrails (Day 7), callable agents (Day 8; a callable run today still expects a decision), dedupe on `alert_id` (Day 16), traces exported (Day 23).

---

### Day 4 — `ingestion` + end-to-end hot path ✅

**Goal**: a real HTTP request produces an agent decision, through the full pipeline.

- `ingestion`: `POST /alerts`, request validation, publish `alert.received` (envelope already carries `app_id`, hardcoded to `"it-ops-triage"` for now — generalized Day 5), return `202` with an alert ID immediately.
- Wire the full chain: `ingestion` → Kafka → `orchestrator` → `tool-gateway` → Kafka `alert.decided`.
- Partition key rule from the start (`docs/ARCHITECTURE.md` §8): every Kafka message is keyed `{app_id}:{alert_key}`, producers are idempotent, and topics are created with multiple partitions (e.g. 12) rather than the default 1. `orchestrator` processes one message at a time per key and commits offsets only after handling. (`alert_key` is built from the it-ops payload for now; Day 5 generalizes it via `alert_key_fields`.)
- Temporary `GET /alerts/{id}` debug endpoint on `ingestion` (throwaway, superseded by `review-console` on Day 9) so results are observable before the review UI exists.
- First rough latency read (not the real target yet — just a baseline).
- **Unit tests**: `ingestion` request validation (malformed payloads rejected with clear `4xx` before anything touches Kafka), Kafka message (de)serialization round-trip, message key is `{app_id}:{alert_key}`.

**Definition of done**: `curl -X POST /alerts` with a real payload results in an observable decision within the chain, with a first latency number recorded (even if rough).

**As built** (notes for later days):
- Request shape: `POST /alerts` takes the `RunAgentRequest` envelope (`source`, `severity`, `message`, optional `timestamp` with a UTC offset) plus `payload`, the app event. `alert_id` (UUID), `alert_key`, and `app_id` are set by `ingestion`; a body that tries to set them is a `422`. Response `202 {alert_id, app_id, alert_key, status}`.
- No it-ops code in `ingestion`: the payload is validated against `backend/apps/it-ops-triage/event_schema.json` (JSON Schema 2020-12, `jsonschema`), and `alert_key` is built from `alert_key_fields: [alert_type, host]`, both read from the app's `manifest.yaml` by `app/core/apps.py` (`FileAppStore`). Day 5 only changes where the manifest comes from (`registry`) and the route (`POST /apps/{app_id}/events`); `app_id` is `DEFAULT_APP_ID` until then.
- Rejections before Kafka: envelope `422` (pydantic), payload over `MAX_PAYLOAD_BYTES` (32 KiB serialized) `413`, schema violation `422` with `{path, message}` per error, unusable `alert_key` field `422`.
- Publishing is inline (`app/kafka/publisher.py`, idempotent, `acks=all`): `202` means Kafka has it; Kafka unreachable → `503` + `Retry-After: 5` after the 10 s send timeout. Day 15's outbox replaces this.
- Known gap, closed by Day 15: a `503` from a send *timeout* is ambiguous. Verified by stopping Kafka: the alert that got `503` stayed queued in the producer and was delivered and decided once Kafka returned, so a client retry creates a second alert (new `alert_id`, which Day 16's `alert_id` dedupe won't catch). aiokafka only expires queued batches when the partition has no known leader, not during a plain broker outage. With the outbox, `202`/`503` depends on a Postgres commit, not on Kafka.
- Topics: the one-shot `kafka-init` Compose service (`backend/local/kafka/create-topics.sh`) creates `alert.received`, `alert.decided`, `verdict.recorded`, `alert.received.dlq` with 12 partitions, and grows existing smaller ones; broker auto-create is now off. `ingestion` and `orchestrator` wait for it (`service_completed_successfully`). The Day 18 kind setup needs an equivalent Job.
- Fixed: Kafka's data wasn't on its volume (the image writes to `/tmp/kraft-combined-logs`), so recreating the container lost topics and offsets. `KAFKA_LOG_DIRS=/var/lib/kafka/data` now.
- Debug `GET /alerts/{id}` (`app/kafka/decisions.py`): in-memory, per-replica, last 10k alerts, fed by a group-less `alert.decided` consumer reading from the start. Shows `pending`/`decided`, the decision, reasons, tool calls, and `latency_ms` (accept → decision seen). After an `ingestion` restart, decisions come back but without `alert_key`/`latency_ms` (no accept record). Delete on Day 9.
- Integration tests, opt-in with `uv run pytest -m integration` while the stack is up (default runs exclude them): `orchestrator/tests/integration/test_kafka_pipeline.py` runs `AlertPipeline` against the real Kafka with the fake LLM on throwaway topics (order per key, commit after publish, LLM down → `ESCALATE`, undecodable message skipped and committed); `ingestion/tests/integration/test_hot_path.py` goes through the whole stack with the real LLM.
- First latency read (`gpt-5.4-mini`, one tool call each, 8 mixed it-ops alerts): sequential p50 2.6 s (2.1–2.9 s); a burst of 8 at once p50 10.6 s, max 18.6 s, because `orchestrator` handles every message strictly one at a time, not just one at a time per key. ADR-0002 allows different keys concurrently; worth doing before Day 21's simulator or Day 26's load test, or with replicas on Kubernetes (12 partitions now allow up to 12).
- The same alert can get a different decision on different runs (e.g. a `healthcheck_flap` with thin data: `SUPPRESS` once, `ESCALATE` once). Expected from an LLM at default settings; Day 7's guardrails bound the risky direction, and Day 25's evals measure it.

---

### Day 5 — `registry`: capability discovery goes live + App Manifest ✅

**Goal**: `orchestrator` discovers its tools from `registry` instead of a hardcoded list, and the platform gains a first-class **App** concept so multiple use cases (three in this build) can share the core without code changes.

- `registry`: REST API to register/list agents and tools (name, version, description, input/output schema), plus a new **App Manifest** resource: `App { app_id, display_name, agents: [{agent_id, version, role: "entry"|"callable", prompt_ref, tool_allowlist[], invoke_on[]}], tools: [{tool_id, version, scope: "app"|"global"}], event_schema_ref, alert_key_fields[], memory_namespace, escalate_when[] }`. Tool registrations carry `read_only: true` (`docs/ARCHITECTURE.md` §2, §13 T8).
- Manifest source of truth is `backend/apps/it-ops-triage/manifest.yaml` (in git, next to the code it references), wrapping the one agent (`triage-agent`, role: `entry`) and the one tool (`lookup_runbook`) from Days 2–3. New `backend/scripts/register_app.py {app_id}` reads it and upserts it into `registry` (idempotent). This is how every app gets registered — see `docs/ARCHITECTURE.md` §12.
- `registry` validates on register and rejects with a clear `4xx`: unknown `tool_id`/version, not exactly one `entry` agent, a `tool_allowlist` entry the manifest doesn't declare, `invoke_on` set on the entry agent, a callable agent with empty `invoke_on`, an `invoke_on` value that isn't a `Decision` enum value, empty `alert_key_fields`, an `escalate_when` rule on a `context.*` field that isn't in `GetContextResponse`, a tool registered without `read_only: true`. (`escalate_when` is used from Day 7 and `invoke_on` from Day 8; validating them now keeps the manifest shape stable.) (`prompt_ref`/`event_schema_ref` can't be checked here — they're files in other services' images; a missing one is an explicit per-app error at resolve time.)
- `orchestrator` queries `registry` by `app_id` to resolve the app's manifest, cached in-memory with a 30s TTL (`docs/ARCHITECTURE.md` §3), then filters tools by that app's `tool_allowlist` to build its tool-call definitions. `tool-gateway` re-checks the allowlist on every call using the `app_id`/`agent_id` from the request context (defense in depth — a tool missing from the definitions still can't be called).
- `ingestion` route becomes app-scoped: `POST /apps/{app_id}/events` (replaces Day 4's `POST /alerts`), resolves that app's manifest from `registry` (same 30s TTL cache as `orchestrator`, so the two never disagree for longer than one TTL) to validate the payload against its `event_schema_ref`, builds `alert_key` from the manifest's `alert_key_fields` (joined with `:`), puts the full validated event into `RunAgentRequest.payload` (`google.protobuf.Struct`, `docs/ARCHITECTURE.md` §5), and stops hardcoding `app_id: "it-ops-triage"` on the published `alert.received` envelope — this is the change Day 4 deferred, and what Day 22's "run all three apps concurrently" depends on.
- **Unit tests**: `registry` CRUD for both agents/tools and App Manifests, `registry`'s manifest validation (each rejection case above), `register_app.py` idempotency (same file twice → no change), `orchestrator`'s discovery logic against a mocked `registry` client (including per-app tool filtering), `ingestion`'s per-app schema validation (a payload valid for one app's `event_schema_ref` rejected under a different `app_id`), `alert_key` construction from `alert_key_fields` (field order respected; a missing field → explicit `4xx`), payload round-trip into `RunAgentRequest.payload` and back (nested objects, arrays, numbers-as-doubles).

**Definition of done**: changing a tool's entry in `registry` (e.g., disabling it) changes what `orchestrator` sees on its next fetch, without a code change or redeploy of `orchestrator`; a second, throwaway App Manifest in a test proves `orchestrator`'s tool set differs per `app_id`; `POST /apps/it-ops-triage/events` works end-to-end and a request against a made-up `app_id` with no manifest is rejected with a clear `4xx`; `it-ops-triage` is registered by running `register_app.py` against its checked-in `manifest.yaml`, not by a hand-written REST call or SQL insert.

**As built** (notes for later days):
- Storage: `tools` (PK `tool_id`+`version`, `read_only` `CHECK`ed true, `enabled`) and `apps` (manifest as jsonb) in `init.sql`, documented in `docs/ARCHITECTURE.md` §8. `init.sql` is idempotent: on an existing volume, apply it with `docker compose exec -T postgres psql -U platform -d platform < postgres/init.sql` instead of `down -v`.
- `registry` API: `PUT`/`GET`/`PATCH {"enabled"}`/`DELETE /tools/{tool_id}/versions/{version}` (delete is `409` while a manifest declares it; disable instead), `PUT`/`GET`/`DELETE /apps/{app_id}`, `GET /apps`, `GET /agents`. `PUT` answers `201`/`200` with `{created, changed}`; an unchanged body is `changed: false`, and re-registering a tool never re-enables it. Agents aren't registered on their own: they exist only inside their app's manifest, and `GET /agents` lists them from there.
- Validation (`app/core/validation.py`) reports every problem in one `422`, each `{field, message}`. Beyond the plan's list, it also rejects: declaring another app's app-scoped tool, a scope mismatch with the registration, duplicate agent/tool ids, `app_id` ≠ URL, and unknown manifest fields (`extra="forbid"`). `context.*` paths are checked against the `GetContextResponse` descriptor and may go one level into a window (`context.window_24h.escalation_count`); `payload.*` paths can't be checked here.
- `GET /apps/{app_id}` is the manifest **resolved**: each agent carries `tools`, its allowlist minus undeclared or disabled tools, with description and input schema. `orchestrator` builds the LLM's tool definitions from that list (it no longer calls `tools/list`), and `tool-gateway` re-checks every call against the same list, so the two can't disagree.
- `ap-shared` gained `registry_client` (`RegistryClient`, httpx): the one 30s TTL cache all three readers use (`MANIFEST_TTL_S`). 404s are cached too, so **a newly registered app is accepted by `ingestion` only once its cached 404 expires (≤30s)**. When registry is unreachable, an expired copy is served (logged); with no copy, `ingestion` answers `503` + `Retry-After`, `orchestrator` publishes ESCALATE "not evaluated: registry unavailable", and `tool-gateway` answers `registry_unavailable` (fails closed; `orchestrator` treats it as an infra failure → ESCALATE).
- `tool-gateway` now refuses calls without a run context and calls outside the agent's tools (`tool_not_allowed`, checked before the tool is even looked up), plus an app-scoped tool called for another app. `mcp_call.py` therefore needs a registered app; it sends `--app-id`/`--agent-id` (default `it-ops-triage`/`triage-agent`). `tools/list` is unfiltered.
- `TOOLS` contract gained a required `read_only: True` key (loader rejects anything else), exposed in MCP tool `_meta`. `register_app.py` registers each declared tool from `tool-gateway`'s `tools/list` (description, schemas, `read_only`), then the manifest; a tool version `tool-gateway` doesn't serve fails the run, which enforces "code first, manifest second" (§12). Its `register()` is unit-tested from `registry`'s tests against the in-memory repository.
- `ingestion`: `POST /alerts` is gone (`404`); `POST /apps/{app_id}/events`, unknown app `404`. On first resolving a manifest version it checks that `event_schema_ref` exists and that every `alert_key_fields` entry is a property in it; otherwise that app's events get a `500` "misconfigured". `GET /alerts/{id}` (debug) is unchanged.
- `it-ops-triage` manifest now complete: `memory_namespace: it-ops-triage`, `escalate_when: []`. The envelope's `severity` isn't addressable by `payload.*`/`context.*` rules, so it-ops' guardrails are a Day 7 decision.
- Dropped unused deps: `sqlmodel` (registry uses asyncpg directly), `pyyaml` at runtime in `orchestrator`/`ingestion`.
- Definition of done, verified on the Compose stack: `PATCH` `lookup_runbook` to disabled → after one TTL, with no restart, `mcp_call.py` gets `tool_not_allowed` and the next `service_down` alert is decided with no tool calls (ESCALATE); re-enabled → the next alert calls it again (RB-003). Per-app tool sets: `test_tool_set_differs_per_app_id` (orchestrator) and `test_two_apps_get_different_tool_sets` (registry). `POST /apps/made-up-app/events` → `404`. End-to-end latency with the real LLM: 4.1 s for one alert.

---

## Week 2 — Context, guardrails, delegation, human feedback

### Day 6 — `memory-store`: real context, not a stub ✅

**Goal**: a working gRPC service serving real behavioral context out of Redis, backed durably by Postgres.

- Redis schema: keys prefixed by `app_id`/`memory_namespace` (from the App Manifest), then per alert-source/type, recurrence counts and recent-alert aggregates over rolling windows (1h/24h/7d, matching `GetContextResponse` in `memory_store.proto`) — decision counts now; the `confirmed_incident_count`/`confirmed_noise_count` fields exist from today but stay `0` until Day 10 feeds verdicts in — the alert-triage analogue of Sentinel's velocity aggregates. Namespacing by app now avoids a data migration when apps #2/#3 land.
- Postgres `memory_history` table: durable snapshot on every update (rebuild-from-source-of-truth path), also keyed by `app_id`. *Changed (ADR-0017): an event log, `memory_events`, one row per decision/verdict, since a per-window snapshot can't rebuild a sliding window. Decisions arrive by consuming `alert.decided` (ADR-0016); the namespace comes from `registry` (ADR-0018).*
- `GetContext` gRPC handler: takes `app_id` in the request, reads Redis, falls back to computing from Postgres on a cache miss and repopulates Redis.
- `backend/scripts/seed.py`: populate Redis + Postgres with synthetic historical alerts.
- **Unit tests**: aggregate window boundary math (edge-of-window cases, empty-history alert types), the cache-miss-falls-back-and-repopulates path.

**Definition of done**: a gRPC test client against `memory-store` returns real, correct aggregates for seeded alert types; `GetContext` latency measured (should be low-single-digit ms).

**As built** (notes for later days):
- Three decisions, each an ADR: decisions reach `memory-store` by consuming `alert.decided` in its own group, and `RunAgentResponse` gained `alert_key = 7` (ADR-0016); memory is an event log, `memory_events`, replacing the `memory_history` snapshot table (ADR-0017); the Redis prefix `memory_namespace` is looked up from `registry` (ADR-0018). `ARCHITECTURE.md` §4/§6/§8/§12 updated.
- `memory_events`: one row per event, `kind` `decision:<Decision>` (verdicts from Day 10), `ref_id` = `alert_id`, unique per (`app_id`, family, `ref_id`), so a redelivered `alert.decided` is counted once. A decision's time is the Kafka message timestamp. On an existing volume `memory_history` was dropped by hand (it was empty) and `init.sql` re-applied.
- Redis: `mem:{ns}:{alert_key}:events` (sorted set, 7 days, trimmed on read and write) and `:facts` (`seen`, `confirmed_incident`, `loaded`), both TTL 8 days. A key without `loaded` is a miss → rebuilt from Postgres. Writers and rebuilds only add (ZADD, true-only HSET), so a rebuild racing a write keeps the write. Redis down → answered from Postgres; a failed cache write invalidates the key.
- Window maths is one pure function (`app/core/events.py`) used for both paths: windows `(now − w, now]`; an event exactly `w` old is out; future-stamped events (clock skew) count. AUTO_RESOLVE and "not evaluated" decisions count as alerts only. `is_novel_alert` = no decision ever; `has_confirmed_incident_history` is all-time.
- gRPC on `:50051` (host `:50053`). `INVALID_ARGUMENT` (missing field), `NOT_FOUND` (unknown app), `UNAVAILABLE` (registry down with nothing cached, or Postgres down). The consumer retries on registry/Postgres errors without committing; it skips (and commits) messages without `alert_key` — which is every `alert.decided` published before this change.
- `orchestrator` sets `alert_key` on every response; `ingestion`'s debug view now keeps it after a restart.
- `seed.py` writes synthetic decisions for five it-ops keys at fixed offsets before now, deletes their Redis keys, and prints the expected counts; re-running replaces its own rows (`ref_id` `seed:…`). `get_context.py` is the gRPC test client (`--repeat N` for latency).
- Definition of done, verified on the Compose stack: every seeded key's `GetContext` matched the printed expectation (e.g. `healthcheck_flap:lb-02` 5/12/17 suppressions over 1h/24h/7d; `cert_expiring:api-gw` 0 in every window but not novel). Latency, client-side over one channel: warm (Redis) p50 0.54 ms, p99 0.82 ms over 1000 calls; cold miss (Postgres rebuild + refill) p50 2.3 ms. A real alert through `ingestion` → `orchestrator` turned a novel key into 1 alert / 1 escalation within a second.
- Not used yet: `orchestrator` doesn't call `GetContext` until Day 7.

---

### Day 7 — `orchestrator` uses context; supervisor + guardrails ✅

**Goal**: agent decisions demonstrably change based on retrieved context, low-confidence cases get flagged for a human, and deterministic guardrails bound what the LLM can decide.

- gRPC client from `orchestrator` to `memory-store` (connection reuse, not a channel per call).
- Retrieved context (recurrence history, verdict counts) feeds into the agent's reasoning — **decide and document** whether this is a direct gRPC call before the tool-calling loop or exposed as a tool (`docs/ARCHITECTURE.md` §6 notes this as open).
- Lightweight supervisor step: a confidence signal on the agent's decision; below a threshold, the decision becomes `ESCALATE` regardless of what the agent itself concluded.
- Guardrails step, right after the supervisor (`docs/ARCHITECTURE.md` §5 decision order, §13 T1/T3, ADR-0010): evaluate the manifest's `escalate_when` rules against `payload.*` and `context.*` (the `GetContext` response); any match forces `ESCALATE` with a reason naming the rule. Supervisor and guardrails can only move a decision *to* `ESCALATE`. `it-ops-triage` starts with `payload.severity in [critical]`.
- Prompts treat the platform's own decision counts (`escalation_count`, `suppression_count`) as context, not evidence of noise; only analyst verdicts (`confirmed_noise_count`, Day 10) count as proof — this stops self-reinforcing suppression (§13 T3).
- **Unit tests**: confidence-threshold routing (table-driven), mocked `memory-store` client, guardrail evaluation (table-driven: `payload.*` and `context.*` rules, match / no match / missing field → no match, a guardrail overrides an LLM `SUPPRESS`, a guardrail never changes an `ESCALATE`), decision order (LLM → supervisor → guardrails).

**Definition of done**: the same alert type produces a different decision depending on seeded `memory-store` context (e.g., recurring/known-noise → auto-resolve, novel → escalate); a `critical` it-ops alert is `ESCALATE`d even when the LLM's own answer (forced via a fixture) was `SUPPRESS`, with the guardrail named in `reasons[]`.


**As built** (notes for later days):
- Three decisions, each an ADR: `orchestrator` calls `GetContext` once per run before the loop, not as a tool (ADR-0019); confidence is the agent's own 0–1 value capped by fixed rules, threshold `supervisor.min_confidence` in the manifest, default 0.6 (ADR-0020); `escalate_when` rules can read the envelope as `alert.*`, because severity isn't in the payload, so it-ops' guardrail is `alert.severity in [critical]`, not `payload.severity` (ADR-0021, amends ADR-0010). `ARCHITECTURE.md` §3/§4/§5/§6/§12 updated.
- `RunAgentResponse.confidence = 8`: the final, capped value; 0 for "not evaluated", an unparseable answer, or no answer within the tool rounds.
- `app/memory/client.py`: one `grpc.aio` channel per process (`MEMORY_STORE_TARGET`, Compose `memory-store:50051`; `MEMORY_STORE_TIMEOUT_S` 1s). Any error status or deadline → `ContextUnavailableError`; the run continues with `{"available": false}` in the prompt, a reason saying so, and confidence capped at 0.5. The channel reconnects by itself after `memory-store` restarts (checked by stopping and starting it).
- Prompt: the alert and the memory context are two data blocks in the first message (every window printed, zeros included). The platform rules (every app) say the decision counts are context, never evidence, and the final answer must carry `confidence`; a missing, non-numeric, boolean or out-of-range confidence doesn't parse, and so escalates. The it-ops prompt says how to weigh recurrence, novelty and incident history.
- `app/agent/supervisor.py` and `app/agent/guardrails.py` are pure functions; `loop.py`'s `respond()` applies them in order after the tool-failure check. Caps: no context 0.5; novel key and not ESCALATE 0.5; SUPPRESS with confirmed-incident history 0.3. Guardrail matching is type-strict (`true` ≠ `1`, `"5"` ≠ `5`, `3 == 3.0`), case-sensitive; a missing or non-scalar field, or any `context.*` rule without context, doesn't match. Every matching rule is named in `reasons[]`, also when the decision was already ESCALATE.
- `registry`: accepts `alert.{source,severity,message,alert_key}` in `escalate_when` and an optional `supervisor: {min_confidence: 0–1}`; serves `supervisor` (null for manifests stored before today). The two `ENVELOPE_FIELDS` lists (`registry` validation, `orchestrator` guardrails) must stay in step.
- `it-ops-triage` manifest: `escalate_when: [alert.severity in [critical]]`, `supervisor.min_confidence: 0.6`. Re-register after pulling.
- Definition of done, verified on the Compose stack with the real LLM after `seed.py`: the same `healthcheck_flap` alert (warning, 1-minute flap, error rate unchanged) was **SUPPRESS** (confidence 0.93) on the seeded `lb-02` and **ESCALATE** on a never-seen host, where the agent also said SUPPRESS but the novel-key cap (0.5 < 0.6) escalated it. The same alert at `critical` severity on `lb-02`: the agent said SUPPRESS, the guardrail forced ESCALATE with `guardrail escalate_when[0] alert.severity = "critical" matched; SUPPRESS changed to ESCALATE.` The forced-SUPPRESS fixture version is `tests/test_decision_order.py`; `tests/integration/test_memory_store_live.py` runs it against the real `memory-store`.
- Known limitation (§13 T3): even with the rule in the prompt, the agent still cited the key's past suppressions or recurrence as support for SUPPRESS in most live runs on `lb-02`, including after the wording was tightened. The decision there rested on the runbook, and nothing the agent writes can lift a cap or a guardrail, but prompt wording isn't a guarantee: Day 25's eval should score reasons that cite the platform's own counts as evidence.
---

### Day 8 — Second agent: `root-cause-summarizer` + `invoke_on` delegation

**Goal**: `it-ops-triage` gains a second, callable-only agent that deepens the explanation on escalated cases without changing the triage decision itself — via a generic delegation mechanism, not a hardcoded call.

- **Decide ADR-0012** (in-process dispatch vs. gRPC self-call for callables) before writing the dispatcher; mark it Accepted or Rejected and update `docs/ARCHITECTURE.md` §5/§9 to match.
- Register a second agent in `it-ops-triage`'s App Manifest: `root-cause-summarizer` (role: `callable`, `invoke_on: ["ESCALATE"]`), its own `prompt_ref` (write a deeper root-cause narrative, not a triage decision) and `tool_allowlist` (`lookup_runbook` + new `recent-changes-lookup`).
- New app-owned tool `recent-changes-lookup(service_or_host, window_start, window_end) -> [{timestamp, type: deploy|config|infra, service, author, summary, ref}]` in `backend/apps/it-ops-triage/tools/`, backed by a static JSON fixture (same approach as Day 2's runbooks) — read-only, and shaped so a real deploy/change source (GitHub, ArgoCD, a CMDB) can replace the fixture later behind the same interface. Allowlisted for `root-cause-summarizer` only; `triage-agent` decides without it. The query window is derived from the alert's own timestamp (e.g., the 2h before it fired).
- Summarizer prompt frames its output as a **probable cause** citing evidence — a change from `recent-changes-lookup` in the window, a runbook match — and says so explicitly when no recent change was found, rather than inventing one.
- `orchestrator`'s `Agent.RunAgent` handler dispatches on `agent_id` (empty → entry agent, existing behavior unchanged).
- Delegation is generic, driven by `invoke_on` (`docs/ARCHITECTURE.md` §3/§5), not hardcoded to `root-cause-summarizer`: after the entry agent's decision is final (after supervisor and guardrails, Day 7), `orchestrator` selects every callable in the manifest whose `invoke_on` contains that decision and runs them **in parallel** with `agent_id` set. Each returns `reasons[]` (`decision` left `DECISION_UNSPECIFIED`); `orchestrator` folds them — each prefixed with its `agent_id`, in manifest order — into the entry agent's final `reasons[]` before publishing `alert.decided`. Callables are never exposed to the entry agent as tools.
- A callable that errors, times out (per-callable timeout), or is over budget doesn't block the decision: `alert.decided` still goes out, with a reason naming the callable that didn't contribute. One direction only: callables never call back or call each other, and their own responses are never published directly.
- **Unit tests**: `agent_id` dispatch routing (empty → entry agent; explicit ID → that agent's handler), reason-folding logic (each callable's `reasons[]` end up prefixed and appended in manifest order, never dropped or double-published), `invoke_on` selection (table-driven: decision × manifests with zero / one / several matching callables — e.g., with `invoke_on: ["ESCALATE"]`, `AUTO_RESOLVE`/`SUPPRESS` trigger nothing), `invoke_on` keys off the *final* decision (agent says `AUTO_RESOLVE`, supervisor or a guardrail raises it to `ESCALATE` → summarizer runs), matching callables run concurrently (two fake callables with delays finish in ~max, not sum), a failing/timed-out callable still yields one `alert.decided` with a "didn't contribute" reason, `recent-changes-lookup` window filtering against its fixture (changes inside / outside / on the edge of the window, no changes found).

**Definition of done**: an `ESCALATE`d alert's final `reasons[]` includes at least one `root-cause-summarizer`-prefixed entry — one that cites a specific fixture change when one exists in the window (e.g., "deploy of `checkout-svc` v2.3.1 20 min before the alert"), and states that no recent change was found when none does; exactly one `alert.decided` event is published per alert regardless of delegation.

---

### Day 9 — `review-console`: human-in-the-loop

**Goal**: escalated alerts are reviewable, and analyst verdicts are recorded.

- `review-console`: Kafka consumer on `alert.decided` (and producer of `verdict.recorded`, keyed `{app_id}:{alert_key}` like every topic — `docs/ARCHITECTURE.md` §8), persists `ESCALATE` decisions into the `cases` table (unique on `(app_id, alert_id)`) (auto-resolved/suppressed alerts are not persisted here — high volume, no human action needed). `cases` carries an `app_id` column from the start.
- REST API: `GET /cases` (list, filterable by `app_id` among other fields), `GET /cases/{id}` (detail, including `reasons[]`/tool-call summary — this is where explainability becomes visible to a human), `POST /cases/{id}/verdict`. `GET /cases` also filters by `alert_key` (used by Day 13's `similar-past-case-lookup`).
- Verdict body: `verdict` (confirmed incident / noise), `verdict_by`, and optional `resolution_notes` — free text on what was actually done to fix it (e.g., "rotated logs, raised disk alert threshold on host-42"). This is the only place the platform learns *how* an alert was resolved; `memory-store` only learns *whether* it was real (`docs/ARCHITECTURE.md` §6).
- On verdict submission: persist it (including `resolution_notes`) on the `cases` row, produce `verdict.recorded` to Kafka. `resolution_notes` stays on the case, not in the Kafka event — `memory-store` consumes counts and flags, not text.
- **Unit tests**: verdict state-machine (a case moves `OPEN` → `RESOLVED` exactly once; a verdict on an already-resolved case is rejected with a clear error), persistence filter logic (only `ESCALATE` decisions land in `cases`), verdict with and without `resolution_notes` both accepted.

**Definition of done**: an escalated alert appears in `GET /cases` with its reasoning visible; submitting a verdict via `POST /cases/{id}/verdict` produces `verdict.recorded`.

---

### Day 10 — Close the feedback loop

**Goal**: analyst verdicts measurably change future agent behavior.

- Extend `memory-store`'s Kafka consumer to handle `verdict.recorded` *(as built on Day 6, this means recording a `verdict:CONFIRMED_INCIDENT` / `verdict:CONFIRMED_NOISE` event in `memory_events` with `ref_id` = `case_id`; the window counts and the all-time flag then follow from `app/core/events.py` with no new counters, ADR-0017)* — increment `confirmed_noise_count` or `confirmed_incident_count` in each window for that `alert_key` (and set `has_confirmed_incident_history` on an incident). That's the mechanism: a confirmed-noise verdict lowers future escalation likelihood for that alert key; a confirmed-real-incident verdict raises it. Update each app's prompt to use these fields explicitly.
- Remove the Day 4 throwaway debug endpoint on `ingestion` now that `review-console` is the real read path.
- **Unit tests**: verdict-driven adjustment logic against a mocked/in-memory Redis (noise verdict → `confirmed_noise_count` up in all windows; incident verdict → `confirmed_incident_count` up and the all-time flag set; a verdict for app A never touches app B's key).

**Definition of done**: re-querying `GetContext` for an alert type after a verdict shows a measurable change in the returned aggregates.

---

## Week 3 — Integration + platform hardening

### Day 11 — Week 2 integration pass (+ buffer)

**Goal**: the full loop works end to end, and drift from Weeks 1–2 is caught. Any Week 2 overrun lands here.

- Manually walk one alert through the entire chain: `ingestion` → `orchestrator` (with `memory-store` context, supervisor, guardrails, summarizer) → `review-console` → verdict → `memory-store` adjustment — observe each stage.
- Fix any schema/contract drift introduced across Days 6–10 (a Day 6 test asserting on something Day 10 quietly changed, etc.).
- Check `docs/ARCHITECTURE.md` §3/§5/§6 against the code so far; fix whichever is wrong.
- **Unit tests**: full regression run across `ingestion`, `orchestrator`, `memory-store`, `review-console`, `registry`, `tool-gateway`.

**Definition of done**: one alert can be traced through the entire feedback loop by hand, and `pytest` is green across every service touched so far.

---

### Day 12 — `tool-gateway`: resilience + sandboxing

**Goal**: a broken tool degrades the platform predictably instead of hanging or crashing it.

- Wrap tool execution with circuit-breaker + retry/backoff, adapted from `ai_microservice_patterns/07_circuit_breaker_retry_backoff`.
- Basic sandboxing: enforce a timeout per tool call, validate tool input against the schema registered in `registry` before execution.
- **Unit tests**: circuit breaker opens after N consecutive failures, retry/backoff timing, timeout enforcement.

**Definition of done**: deliberately break a tool (inject a sleep/error), observe the circuit breaker open, and confirm `orchestrator` degrades gracefully (e.g., escalates by default) rather than hanging.

---

### Day 13 — `registry`: versioning + a global tool + app #2 first cut

**Goal**: adding a new tool, or a whole new app, requires no code change to `orchestrator`.

- Version resolution for registered agents/tools (the `version` field exists since Day 5): `orchestrator` resolves "latest" or a pinned version.
- Register a second real tool, `similar-past-case-lookup`, with `scope: "global"` — the first global tool, implemented in `tool-gateway`'s own package (platform code, not under any app). Backed by `review-console`'s `GET /cases` (REST, filtered by the caller's `app_id` and `alert_key`; no direct read of `review-console`'s table). The `app_id` comes from the MCP request context, never a tool argument, so injected text can't make it query another app (`docs/ARCHITECTURE.md` §13 T4). It returns recent resolved cases with their `decision`, `reasons[]`, `verdict`, and `resolution_notes`, so on a repeat incident the agent's `reasons[]` can say "last time this was fixed by …" — a *suggestion* for the analyst, never an action (`docs/ARCHITECTURE.md` §2). `resolution_notes` is analyst-written free text passed into an LLM prompt: the tool returns it as clearly delimited data, and the prompt treats it as reference material, not instructions. Add it to `it-ops-triage`'s `triage-agent` `tool_allowlist`.
- Register app #2, **`cost-anomaly-triage`** (see `docs/ARCHITECTURE.md` §3), as a minimal first cut under `backend/apps/cost-anomaly-triage/`:
  - `manifest.yaml`: one agent, `cost-triage-agent` (role: `entry`), allowlisting `billing-lookup` and the global `similar-past-case-lookup`; `alert_key_fields: [service, region]`.
  - Event schema: a spend-spike alert (`account`, `service`, `region`, `tags`, `window_start`/`window_end`, `baseline_cost`, `observed_cost`).
  - App-owned tool `billing-lookup(service, region, window) -> cost breakdown by resource/tag`, backed by a static JSON fixture (same no-external-dependency approach as Day 2's runbook data).
  - Prompt: decide `AUTO_RESOLVE` / `ESCALATE` / `SUPPRESS` on the spike.
  - Rollout per `docs/ARCHITECTURE.md` §12: rebuild `ingestion`/`orchestrator`/`tool-gateway` with the new app dir, then `register_app.py cost-anomaly-triage`.
- **Unit tests**: version resolution logic, `registry` rejects a malformed schema on register (both tool and App Manifest registration), `billing-lookup` against its fixture, `cost-anomaly-triage` event-schema validation, `similar-past-case-lookup` never returns another app's cases (including when the LLM-supplied arguments try to name another app) and includes `resolution_notes` when present.

**Definition of done**: the new tool is available to `orchestrator` purely by registering it in `registry` — no `orchestrator` code change, just a restart/refresh of its discovery call; an event posted to `POST /apps/cost-anomaly-triage/events` routes to `cost-triage-agent` with `billing-lookup` + `similar-past-case-lookup` and the `cost-anomaly-triage` memory namespace, never to `it-ops-triage`'s agent, tools, or memory; `triage-agent` cannot see `billing-lookup`.

---

### Day 14 — Rate & cost governance

**Goal**: the platform has a visible, enforced budget instead of unlimited LLM/tool spend.

- Per-`app_id`/`agent_id` token/cost budget tracking (Redis `budget:{app_id}:{agent_id}:{date}`, `docs/ARCHITECTURE.md` §8/§10). Over budget → the run is **rejected** with an explicit reason, never queued: an over-budget entry agent publishes `alert.decided` as `ESCALATE` with "not evaluated: budget exceeded" (so a human still sees the alert); an over-budget callable just doesn't contribute (Day 8).
- Rate limiting on `ingestion`, per `app_id` and per alert source within an app (Redis counters), returning `429` with `Retry-After` — so one app's flood can't starve the others even when each source is under its own limit.
- **Unit tests**: budget counter increment/reset logic, over-budget entry run → `ESCALATE` with the budget reason, over-budget callable → "didn't contribute" reason, rate-limit counters per app and per source, `429` when either limit is hit.

**Definition of done**: artificially lower a budget and observe `orchestrator` reject new agent runs with a clear, explicit reason — not a silent failure, a queue, or a hang; flood one app past its rate limit and confirm the other app's events are still accepted.

---

### Day 15 — Reliability, part 1: outbox + Celery relay

**Goal**: `ingestion` no longer publishes to Kafka directly — every accepted alert is durably recorded first and relayed in order.

- Adapt `ai_microservice_patterns/08_outbox_dead_letter_queue` into `ingestion`'s publish path, per `docs/ARCHITECTURE.md` §10 and ADR-0008:
  - `outbox` table in `init.sql` (§8 schema, including `kafka_key`). `POST /apps/{app_id}/events` now writes the validated event + trace headers there and returns `202` — no direct Kafka publish from `ingestion` anymore.
  - Celery is the relay: add `celery-worker` and `celery-beat` to `docker-compose.yml` with **Redis as the broker** (its own DB index, §8). A beat-scheduled task every ~1s claims `PENDING` rows in `id` order with `FOR UPDATE SKIP LOCKED`, publishes each to `alert.received` keyed by its stored `kafka_key` (`{app_id}:{alert_key}`), marks `SENT`. A Redis lock keeps only one relay run active at a time, so a slow run can't overlap the next tick and reorder a key's messages.
  - Prune `SENT` rows after a retention window (another small beat task).
- **Unit tests**: outbox write-then-relay round trip, two concurrent relay workers never publish the same row twice, the relay lock prevents overlapping runs, rows for one key are published in `id` order with the stored key.

**Definition of done**: alerts posted to both registered apps flow through outbox → relay → Kafka → decision exactly as before, with ordering per key preserved; note the added end-to-end latency from the relay interval.

---

## Week 4 — Reliability, Kubernetes, app #3

### Day 16 — Reliability, part 2: DLQ, dedupe, Kafka-outage test

**Goal**: no alert is silently lost or decided twice when Kafka is briefly unavailable.

- Kafka unreachable → outbox rows stay `PENDING`, no attempt counted. Message-specific failure → `attempts++`; at 5, mark `DEAD` and publish to the new `alert.received.dlq` topic.
- At-least-once delivery means duplicates are possible: `orchestrator` dedupes on `alert_id` via a TTL'd Redis marker (`seen:{app_id}:{alert_id}`, shared across replicas), and `review-console` via the `cases` unique constraint (`docs/ARCHITECTURE.md` §8).
- A small burst script (`backend/scripts/burst.py`, or a loop of hand-posted `POST /apps/{app_id}/events` calls) to drive the outage test — the full simulator comes Day 21 and will replace it.
- **Unit tests**: Kafka-down keeps rows `PENDING` without burning attempts, DLQ routing after 5 injected message failures, `orchestrator`/`review-console` ignore a duplicate `alert_id`.

**Definition of done**: kill Kafka briefly during a burst, restore it, and confirm every alert is eventually published (outbox drains) and decided exactly once; induced message failures land in `alert.received.dlq` rather than vanishing.

---

### Day 17 — Finish app #2 + platform-core review

**Goal**: `cost-anomaly-triage` is a real adapter, and the platform-core-vs-adapter boundary is checked in code, not just asserted.

- Review the codebase: confirm IT-ops-specific code (alert schema, runbook tool, triage prompt) is isolated from platform code (`registry`, `tool-gateway`, `orchestrator`'s agent-loop skeleton, `memory-store`'s generic context API); refactor any leakage found.
- Each app is a module under `backend/apps/{app_id}/` (manifest, event schema, prompts, any app-specific tool), registered via `register_app.py` — no changes to `orchestrator`, `memory-store`, `tool-gateway`, or `registry` code itself.
- **App #2, `cost-anomaly-triage`** (first cut Day 13): finish it as a real adapter. Use `memory-store` context so a recurring known spike (e.g., a monthly batch job on the same `service:region`) is `SUPPRESS`ed instead of re-escalated every cycle. Seed fixture history that shows this. Add its guardrails (`escalate_when`, e.g. on a very large `observed_cost`/`baseline_cost` jump expressed as a payload field).
- **Unit tests**: `cost-anomaly-triage` prompt assembly and guardrails, recurring-spike suppression against seeded history.

**Definition of done**: a seeded monthly batch-job spike is `SUPPRESS`ed with a reason citing its history, while a novel spike on another `service:region` is `ESCALATE`d; the core-vs-adapter review finds no app-specific code in platform services (or fixes what it finds).

---

### Day 18 — Kubernetes, part 1: local cluster with platform parity

**Goal**: the same images that run in Compose run on a local Kubernetes cluster, end to end, before app #3 needs it (ADR-0014).

- Local cluster with **kind**; `backend/scripts/k8s_up.sh` creates it, builds images, loads them with `kind load docker-image`, and applies the manifests. Images are tagged with the git SHA, never `latest`, so every rollout is a real tag change.
- Manifests under `backend/deploy/k8s/` with **Kustomize** (`base/` + `overlays/local/`). One Deployment + Service per platform service (never per app, `docs/ARCHITECTURE.md` §12). Plus `celery-worker`, and `celery-beat` at exactly 1 replica with `strategy: Recreate` (two beats would double-schedule).
- Infra as single-replica StatefulSets with PVCs: Kafka (KRaft), Postgres (`init.sql` from a ConfigMap), Redis. These are dev-grade; managed services are phase two. A topic-creation Job creates `alert.received`, `alert.decided`, `verdict.recorded`, `alert.received.dlq` with a fixed partition count (e.g. 6), which caps `orchestrator` replicas (ADR-0002). The OTel Collector is deployed as a no-op sink; the LGTM stack joins on Day 23.
- Config through ConfigMaps (service URLs, TTLs, limits) and the LLM API key through a Secret created from the local env, never committed.
- `register_app.py` and `seed.py` run against the cluster via `kubectl port-forward` to `registry`/Postgres; register apps #1 and #2.
- Every container gets resource requests/limits, a liveness probe on `/healthz`, and a readiness probe on a new `/readyz` (Day 19 makes `/readyz` meaningful).
- **Unit tests**: a pytest over `kubectl kustomize backend/deploy/k8s/overlays/local` output (plus `kubeconform` schema validation) asserting the invariants: every Deployment has probes, requests/limits, and a non-`latest` image; no Deployment is named after an app; `celery-beat` has 1 replica.

**Definition of done**: `k8s_up.sh` from a clean machine → all pods `Ready`; an `it-ops-triage` and a `cost-anomaly-triage` event posted through a port-forwarded `ingestion` are decided and show up as cases, same as in Compose; Compose still works unchanged.

---

### Day 19 — Kubernetes, part 2: zero-interruption rolling updates

**Goal**: rolling `ingestion`, `orchestrator`, and `tool-gateway` to a new image interrupts no app. This is the mechanism app #3 ships with tomorrow.

- Rollout settings for those three Deployments: `replicas: 2`, `RollingUpdate` with `maxUnavailable: 0`, `maxSurge: 1`, a PodDisruptionBudget (`minAvailable: 1`), and `terminationGracePeriodSeconds` longer than the longest allowed agent run.
- **Readiness means ready** (`/readyz`, separate from `/healthz`): `ingestion` when Postgres (outbox) is reachable; `tool-gateway` when its startup tool scan has finished; `orchestrator` when its Kafka consumer has joined and `tool-gateway`/`registry` are reachable.
- **Graceful shutdown on SIGTERM**: flip `/readyz` to failing, a short `preStop` sleep so the pod leaves Service endpoints, then drain. `ingestion` and `tool-gateway` finish in-flight requests and MCP calls; `orchestrator` stops polling, finishes the current message, commits its offset, and leaves the group cleanly. Applies to Compose `docker stop` too.
- **Kafka**: `orchestrator` commits offsets only after an `alert.decided` publish succeeds and uses the cooperative-sticky assignor, so a pod swap doesn't pause every partition. A message redelivered after a swap is caught by Day 16's `alert_id` dedupe.
- **MCP client**: on a dropped connection, `orchestrator` reconnects and retries the call once against the Service (another pod). This is a connection-level retry on the client side, separate from Day 12's tool-execution retries inside `tool-gateway`, and safe because every tool is read-only (`docs/ARCHITECTURE.md` §2). A second connection failure is an ordinary tool failure (§10): breaker, then escalate-by-default.
- `backend/scripts/rollout_app.sh {app_id}`: build and tag images → `kubectl set image` on the three Deployments → `kubectl rollout status` on all three (fail on timeout) → only then `register_app.py {app_id}`. This is the ADR-0006 order, enforced by the script. With no `app_id` it rolls code only (no registration).
- **Unit tests**: SIGTERM handler flips readiness and waits for in-flight work, `/readyz` returns 503 until the tool scan finishes, offsets are committed only after a successful publish, MCP client retries exactly once on connection loss and never on a tool error.

**Definition of done**: while Day 16's `burst.py` drives apps #1 and #2 at a steady rate, a code-only `rollout_app.sh` rolls all three Deployments with **zero non-`202` responses, zero tool-call failures, and every alert decided exactly once**. A deliberately broken image (failing `/readyz`) stalls its rollout with old pods still serving, and `kubectl rollout undo` restores it.

---

### Day 20 — App #3: `security-alert-triage`

**Goal**: a third, unrelated domain runs on the same core — and the one where payload text is attacker-chosen.

- Under `backend/apps/security-alert-triage/`:
  - `manifest.yaml`: one agent, `security-triage-agent` (role: `entry`), allowlisting the app-owned `ioc-reputation-lookup` and the global `similar-past-case-lookup`; `alert_key_fields: [rule_id, host]`.
  - Event schema: a SIEM/EDR alert (`rule_id`, `severity`, `host`, `src_ip`, `dst_ip`, `user`, `indicators[]` of IP/domain/file-hash).
  - App-owned tool `ioc-reputation-lookup(indicator) -> {verdict: known-bad|known-benign|unknown, source}`, backed by a static JSON fixture — no live threat-intel API calls.
  - Prompt: `ESCALATE` anything with a known-bad indicator or high severity, `SUPPRESS` recurring benign noise (e.g., an internal vulnerability scanner tripping the same rule, recognized via analyst-confirmed noise in `memory-store`), `AUTO_RESOLVE` only for known-benign indicators on low-severity rules.
  - Guardrails (`escalate_when`): `alert.severity in [high, critical]` (ADR-0021: severity is an envelope field) and `context.has_confirmed_incident_history in [true]` — security alerts are where payload text is attacker-chosen, so these are never left to the LLM alone (`docs/ARCHITECTURE.md` §13 T1/T3).
  - **First app shipped on Kubernetes**: `rollout_app.sh security-alert-triage` (Day 19). Rolling update of `ingestion`/`orchestrator`/`tool-gateway`, then registration, while `burst.py` keeps driving apps #1 and #2.
- **Unit tests**: `ioc-reputation-lookup` against its fixture, `security-alert-triage` event-schema validation, guardrails fire on high/critical severity and on confirmed-incident history.

**Definition of done**: a known-bad-IOC alert is `ESCALATE`d citing the IOC verdict; a high-severity alert whose payload text urges suppression is still `ESCALATE`d with the guardrail named; standing up app #3 added no Deployments and no platform code, and apps #1 and #2 saw zero non-`202` responses and zero tool-call failures during its rollout.

---

## Week 5 — Three apps concurrently, observability, evals

### Day 21 — Alert simulator

**Goal**: realistic, mixed, reproducible traffic for all three apps, standing in for the alert sources that live outside the platform.

- `backend/scripts/simulate_alerts.py` posts schema-valid events to `POST /apps/{app_id}/events` for all three apps (real generators — monitoring tools, cost-anomaly detectors, SIEMs — are outside the platform, `docs/ARCHITECTURE.md` §2). Replaces Day 16's burst script.
- Scenario mix per app, so every decision path is exercised: **repeats** (same `alert_key` at a steady cadence), **noise** (known-benign — scanner hits, monthly batch-job spikes), **real-looking incidents** (novel `alert_key`, high severity, known-bad IOC), **change-correlated incidents** (it-ops alerts timed just after a deploy in the `recent-changes-lookup` fixture, so `root-cause-summarizer` has something to find), and **adversarial** scenarios (`docs/ARCHITECTURE.md` §13): prompt-injection text in payload fields (command lines, usernames, file names, user agents) urging `SUPPRESS`, and memory poisoning (a long run of benign-looking alerts on one `alert_key`, then a real-looking attack on the same key).
- Scenario definitions live with each app (`backend/apps/{app_id}/simulator/`), not in the script — the script stays app-agnostic and discovers apps the same way `tool-gateway` does (`docs/ARCHITECTURE.md` §12), so app #4 gets simulated traffic by adding a folder.
- Knobs: `--apps`, `--rate` (events/sec per app), `--duration`, `--mix` (scenario weights), `--seed` (deterministic replay for demos and debugging), `--collide-keys` (reuse `alert_key` values across apps).
- Every event is tagged with its scenario and expected decision (in a header, not the payload), so a run can report expected-vs-actual decisions per app — a cheap live sanity check, and the seed for Day 25's eval sets.
- **Unit tests**: every simulator scenario produces an event that passes its app's event schema, same `--seed` → identical event sequence, scenario discovery from a fixture apps dir.

**Definition of done**: one `simulate_alerts.py` run against all three apps produces a per-app expected-vs-actual report; adversarial scenarios that should escalate do.

---

### Day 22 — Three apps concurrently (+ buffer)

**Goal**: the platform-core-vs-domain-adapter boundary is proven by running all three apps side by side under load. Any Week 4 overrun (including the Kubernetes days) lands here.

- Run on the kind cluster (Compose as a second check). Run all three apps' events through the shared pipeline concurrently, driven by `simulate_alerts.py` (security alerts at the highest rate), and confirm no cross-app leakage: memory context, tool access, cases, budgets, and rate limits stay scoped to each `app_id`. Include deliberately colliding `alert_key` values across apps (`--collide-keys`).
- Check `docs/ARCHITECTURE.md` §3/§4/§12 against the code; fix whichever drifted.
- **Unit tests**: full regression run across all three registered apps.

**Definition of done**: `it-ops-triage`, `cost-anomaly-triage`, and `security-alert-triage` are simultaneously registered and each produces correctly-scoped decisions, memory context, and cases through the identical platform code path; each app's agent sees only its own app tools plus allowlisted global ones; standing up apps #2/#3 added no containers to `docker-compose.yml` and no Deployments to `backend/deploy/k8s/` — only an image rebuild of `ingestion`/`orchestrator`/`tool-gateway` plus manifest registration (`docs/ARCHITECTURE.md` §12).

---

### Day 23 — Full OTel pipeline

**Goal**: one alert's journey is visible as a single connected trace.

- Point every service's OTel SDK at the real Collector (swap from stdout-only); Collector fans out to Tempo (traces), Mimir (metrics), Loki (logs). The Collector → backends half already runs from Day 1; only the service side is new. Mind FastAPI ≥0.142's automatic telemetry: setting `OTEL_EXPORTER_OTLP_ENDPOINT` makes it attach its own OTLP/HTTP (`http/protobuf`, Collector port `4318`) exporters. Either rely on that or pass `telemetry={'auto_configure': False}` to `FastAPI()` and configure exporters in `setup_observability()` — not both, or spans export twice. Deploy the same stack to the kind cluster (Day 18 left only a no-op Collector there) and add a rollout panel: per-app `202` rate and tool-error rate during a `rollout_app.sh` run.
- Propagate trace context through Kafka message headers so a trace spans `ingestion` → Celery outbox relay → `orchestrator` → `tool-gateway` (MCP) / `memory-store` (gRPC) → `review-console` as one connected trace (the outbox row stores the trace headers, Day 15).
- Grafana dashboards: RED metrics per service, Kafka consumer-lag, outbox backlog and DLQ count, and a domain dashboard broken out **per `app_id`** (decision distribution auto-resolve/escalate/suppress, guardrail hits, end-to-end latency, `429`s) — all apps share one `orchestrator`, so a noisy app has to be visible as such.
- Scope: this stack observes the platform only — never alert sources or the systems they monitor (`docs/ARCHITECTURE.md` §7).
- **Unit tests**: trace-context propagation round-trip (a trace ID injected into a Kafka header comes back unchanged on the consumer side).

**Definition of done**: fire one alert through the full system and find it as a single connected span tree in Tempo, with correlated logs in Loki.

---

### Day 24 — LLM-specific observability

**Goal**: cost and token spend are visible per agent call, not invisible.

- Custom OTel span attributes on every LLM call: model, prompt tokens, completion tokens, cost estimate, tool-call count, latency.
- Grafana dashboard: cost/token burn rate over time and tool-call volume by tool, broken out per `app_id` and `agent_id` (the same keys Day 14's budgets use), plus budget consumption vs. limit.
- **Unit tests**: span-attribute population logic against a mocked LLM response.

**Definition of done**: the dashboard shows real token/cost numbers from a simulator run across all three apps.

---

### Day 25 — Eval harness

**Goal**: a prompt or logic regression is caught automatically, not by eyeballing a demo.

- A small labeled fixture set per app (synthetic alerts with expected decisions — reuse Day 21's simulator scenarios as the starting point) checked into `backend/apps/{app_id}/`, including an **adversarial set** per app (§13 T1–T3: injection in payload, injection in `resolution_notes`, memory poisoning).
- Eval script: run `orchestrator` against the fixtures, report per-app accuracy plus precision/recall per decision (`AUTO_RESOLVE` / `ESCALATE` / `SUPPRESS`) — missed escalations are the costliest error, so report `ESCALATE` recall explicitly — and flag regressions. Adversarial cases are a hard gate, not an average: any injected or poisoned case that ends `SUPPRESS`/`AUTO_RESOLVE` when it should escalate fails the run.
- Wire as a Celery task (batch re-eval trigger), mirroring Sentinel's batch re-scoring shape — scheduled nightly by the `celery-beat` added Day 15, and runnable on demand.
- **Unit tests**: the eval scoring logic itself (given known predictions vs. labels, correct metrics computed), the adversarial hard gate fails on a single miss.

**Definition of done**: the eval script reports a baseline accuracy number per app locally; deliberately regressing the prompt/logic causes the eval to catch it.

---

## Week 6 — Load + polish

### Day 26 — Load & resilience

**Goal**: the platform's behavior under load and under failure is measured, not assumed.

- Run on the kind cluster, where replicas are real. Load test at a sustained rate against `POST /apps/{app_id}/events` across all three apps, using `simulate_alerts.py` (Day 21) for realistic mixed traffic — either directly at higher `--rate`, or as the event generator inside a k6/locust harness. Capture p50/p95/p99 per `app_id`, not just overall, so one noisy app slowing the others is visible.
- One deliberate failure scenario (kill `memory-store` or `tool-gateway` mid-load), observed through Grafana/Tempo — confirm `orchestrator` degrades predictably (e.g., escalate-by-default) rather than silently misbehaving.
- **Unit tests**: none new — full regression suite run.

**Definition of done**: documented load-test numbers and one documented, observed failure-mode behavior.

---

### Day 27 — Polish, docs, buffer

**Goal**: a stranger (or future-you) can pick this up from a clean checkout.

- Fill in `CLAUDE.md`'s status section with real build/run/test commands.
- README: what it is, how to run it (`docker compose up`, `k8s_up.sh` and `rollout_app.sh`, seed script, `register_app.py`, `simulate_alerts.py` for a live demo, example `curl`), links to `docs/ARCHITECTURE.md`, `docs/adr/`, and `docs/ENTERPRISE_READINESS.md`.
- Record the measured numbers (eval accuracy and `ESCALATE` recall per app, p95 latency per app, cost per 1,000 alerts per app) in the README.
- Buffer time for whichever day ran over — treat Day 27 morning as unscheduled slack, not additional scope.
- **Unit tests**: no new logic — full suite across all 6 services plus Celery as a single regression pass.

**Definition of done**: documented load-test numbers against a stated target, one documented failure-mode behavior, a README that lets a stranger run the whole stack from a clean checkout, and a full `pytest` run passing green.

---

## Explicitly out of scope for this build (phase-two candidates)

Planned in order in `docs/ENTERPRISE_READINESS.md` §8.

- Production Kubernetes: a managed/multi-node cluster, Helm packaging, HPA, managed Kafka/Postgres/Redis, GitOps. This build has a local kind cluster with Kustomize (Days 18–19, ADR-0014) — enough to prove zero-interruption app rollouts, not to run production.
- Agent-to-agent (A2A) communication *across apps* — apps registered via the App Manifest (Day 5) are isolated tenants sharing platform infrastructure (Kafka/gRPC/REST/MCP), not agents calling each other directly. A dedicated A2A protocol is validation work for later. (Within-app delegation from an entry agent to a callable-only agent, e.g. `triage-agent → root-cause-summarizer` on Day 8, is in scope — that's a fixed edge in one app's own manifest, not agent discovery.)
- A dynamic/self-serve app-registration UI or workflow — apps #2-3 are registered by hand with `register_app.py` (Day 13, Day 20), not through a built admin flow.
- Automated remediation — agents and tools are read-only; `AUTO_RESOLVE` closes the alert, not the problem (`docs/ARCHITECTURE.md` §2, §11).
- Anomaly detection — alerts are raised by external systems; the platform only triages them.
- Real data connectors and a per-app credentials model — every app-owned tool reads a fixture in this build.
- Per-customer (org) tenancy — a tenant is an app.
- Callable → callable agent calls, or LLM-chosen delegation (`invoke_on` decides, Day 8).
- A real trained classifier replacing/augmenting the agent's reasoning.
- Security controls that gate real data (`docs/ARCHITECTURE.md` §13 phase-two items): authentication on `ingestion` (per-source credentials bound to an `app_id`) and on the analyst (`review-console`) API, mTLS between services and Kafka ACLs, process isolation for app-owned tools, redaction of sensitive fields before prompts and traces. Acceptable to skip only because this build runs locally on synthetic data.
- A frontend (this build stays API + Grafana only).
