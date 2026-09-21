# Build Plan: 4-Week / 20-Day Sequence

Status: planning doc, pre-implementation beyond partial service skeletons. Implements the design in `docs/ARCHITECTURE.md`. Each day builds on a *runnable* system from the day before — nothing is "wire it all up at the end."

Service map this plan assumes (see conversation / `docs/ARCHITECTURE.md` for full rationale): `ingestion`, `orchestrator`, `memory-store`, `review-console`, `registry`, `tool-gateway`, plus Celery workers. The platform is multi-app: 2-3 configurable use cases (products) run on the same shared core, each registered as an **App Manifest** in `registry` (introduced Day 5) rather than hardcoded. App #1 / reference domain adapter: IT ops alert triage (`ingestion` accepts alerts; `orchestrator`'s agent decides auto-resolve / escalate / suppress-as-noise). Apps #2-3 are registered Day 12 and stood up as real adapters Day 15. Agent-to-agent (A2A) communication between apps is explicitly out of scope — apps are isolated tenants sharing infrastructure (Kafka/gRPC/REST/MCP transports), not a mesh.

## Cross-cutting rules for every day

- **Instrument as you build, not at the end.** Each service gets OpenTelemetry SDK + structured logging the day it's created, even before the collector/backends exist (logs to stdout, traces no-op'd). Day 16 wires the *pipeline*, not five services' instrumentation at once.
- **Every day ends with something running in Docker Compose.** If a day's work isn't runnable via `docker compose up`, it isn't done.
- **Proto contracts are written before the services that implement them** — they're the interface, not an afterthought.
- **Unit tests are part of each day's work, not a separate pass.** Every day ends with a "Unit tests" step for the logic written that day; that day isn't done until those tests pass locally (`pytest`). Integration-style plumbing (gRPC/Kafka wiring) is covered by each day's "Definition of done" check — unit tests target the pure logic underneath (routing, scoring/threshold math, window aggregates, validation), which is what's easy to get subtly wrong and hard to catch by eyeballing a demo.
- **Commit at the end of each day** with the system in a working state, so any day can be a checkpoint to roll back to.
- **Platform core vs. domain adapter stays a live distinction, not just a doc section.** Anything specific to IT-ops (alert schema, runbook-lookup tool, the triage prompt) lives in a clearly separate module from platform code (registry, tool-gateway, orchestrator's agent-loop skeleton, memory-store's generic context API) — the whole point of this project is that a second (and third) domain can plug in without touching the platform core.
- **App identity is explicit, everywhere.** Every event envelope, gRPC request, and persisted row that's app-specific carries an `app_id`. Nothing about routing to the right agent, tool set, or memory namespace is inferred — it's looked up from that app's App Manifest in `registry`.

---

## Week 1 — Agent fundamentals

### Day 1 — Foundation: infra, contracts, skeletons

**Goal**: `docker compose up` brings up every piece of infrastructure the platform needs, and all 6 services register as empty-but-healthy.

- Repo scaffold: `backend/services/{ingestion,orchestrator,memory-store,review-console,registry,tool-gateway}`, `backend/proto/`, `backend/local/`, `docs/`.
- `backend/local/docker-compose.yml`: Kafka (KRaft mode), Redis, **local Postgres** (not hosted — simpler for solo dev), OTel Collector, Loki, Mimir, Tempo, Grafana — all with health checks.
- `backend/local/postgres/init.sql`: minimal `cases` and `memory_history` schemas.
- Proto contracts: `memory_store.proto` (`GetContext`), `agent.proto` (`RunAgent`) in `backend/proto/`. Both carry an `app_id` field from day one, even though only one app exists yet — avoids a breaking-change rework when Day 5 introduces multi-app config. Generate Python stubs into `backend/shared/proto_gen/`, imported by both `orchestrator` and `memory-store`.
- Each of the 6 FastAPI services: skeleton app, `/healthz`, Dockerfile, OTel SDK wired to log to stdout (collector not yet consuming).
- **Unit tests**: request/response schema validation for each service's skeleton endpoints; a test that generated proto stubs import cleanly and match `.proto` field names.

**Definition of done**: `docker compose up` → all containers healthy, all 6 services respond `200` on `/healthz`, Grafana loads (empty dashboards okay), `pytest` passes across all service packages.

---

### Day 2 — `tool-gateway`: first real MCP tool

**Goal**: a working MCP server exposing one real tool, callable by anything that speaks MCP.

- Implement an MCP server in `tool-gateway` exposing `lookup_runbook(alert_type) -> RunbookEntry`.
- Seed static/in-memory runbook data (JSON fixture) — no external dependency yet.
- Tool schema (input/output) defined explicitly, not inferred — this schema is what `registry` will later store.
- **Unit tests**: tool input schema validation, lookup logic (found / not-found cases).

**Definition of done**: a standalone MCP test client (or simple script) calls `tool-gateway` and gets a real runbook entry back for a seeded alert type, and a clear "not found" for an unseeded one.

---

### Day 3 — `orchestrator`: agent core with tool-calling

**Goal**: an agent that receives an alert, calls a real tool via `tool-gateway`, and produces a decision with reasons.

- LLM client integration (tool-use / function-calling format) inside `orchestrator`.
- `RunAgent(Alert) -> AgentDecision` gRPC handler: builds tool definitions from `tool-gateway`, runs the tool-calling loop, returns `{decision, reasons[]}` — `reasons[]` is the explainability contract, same role as Sentinel's rule-names field, now populated by the agent's tool-call trace.
- Kafka consumer on `alert.received` (no producer yet — test by hand-publishing).
- **Unit tests**: decision-parsing logic, tool-call routing against a **mocked** `tool-gateway` client (no live LLM or network calls in unit tests — use fixture responses).

**Definition of done**: hand-publish an `alert.received` event, observe `orchestrator` consume it, call `tool-gateway` for real, and produce an `alert.decided` event with a populated `reasons[]`.

---

### Day 4 — `ingestion` + end-to-end hot path

**Goal**: a real HTTP request produces an agent decision, through the full pipeline.

- `ingestion`: `POST /alerts`, request validation, publish `alert.received` (envelope already carries `app_id`, hardcoded to `"it-ops-triage"` for now — generalized Day 5), return `202` with an alert ID immediately.
- Wire the full chain: `ingestion` → Kafka → `orchestrator` → `tool-gateway` → Kafka `alert.decided`.
- Temporary `GET /alerts/{id}` debug endpoint on `ingestion` (throwaway, superseded by `review-console` on Day 8) so results are observable before the review UI exists.
- First rough latency read (not the real target yet — just a baseline).
- **Unit tests**: `ingestion` request validation (malformed payloads rejected with clear `4xx` before anything touches Kafka), Kafka message (de)serialization round-trip.

**Definition of done**: `curl -X POST /alerts` with a real payload results in an observable decision within the chain, with a first latency number recorded (even if rough).

---

### Day 5 — `registry`: capability discovery goes live + App Manifest

**Goal**: `orchestrator` discovers its tools from `registry` instead of a hardcoded list, and the platform gains a first-class **App** concept so 2-3 use cases can share the core without code changes.

- `registry`: REST API to register/list agents and tools (name, version, description, input/output schema), plus a new **App Manifest** resource: `App { app_id, display_name, agents: [{agent_id, version, role: "entry"|"callable", prompt_ref, tool_allowlist[]}], tools: [{tool_id, version, scope: "app"|"global"}], event_schema_ref, memory_namespace }`.
- Seed `registry` with one App Manifest (`app_id: "it-ops-triage"`) wrapping the one agent (`triage-agent`, role: `entry`) and the one tool (`lookup_runbook`) from Days 2–3.
- `orchestrator` queries `registry` (startup or per-call, document the choice) by `app_id` to resolve the app's manifest, then filters tools by that app's `tool_allowlist` to build its tool-call definitions.
- `ingestion` route becomes app-scoped: `POST /apps/{app_id}/events` (replaces Day 4's `POST /alerts`), resolves that app's manifest from `registry` to validate the payload against its `event_schema_ref`, and stops hardcoding `app_id: "it-ops-triage"` on the published `alert.received` envelope — this is the change Day 4 deferred, and what Day 15's "run 2-3 apps concurrently" depends on.
- **Unit tests**: `registry` CRUD for both agents/tools and App Manifests, `orchestrator`'s discovery logic against a mocked `registry` client (including per-app tool filtering), `ingestion`'s per-app schema validation (a payload valid for one app's `event_schema_ref` rejected under a different `app_id`).

**Definition of done**: changing a tool's entry in `registry` (e.g., disabling it) changes what `orchestrator` sees on its next fetch, without a code change or redeploy of `orchestrator`; a second, throwaway App Manifest in a test proves `orchestrator`'s tool set differs per `app_id`; `POST /apps/it-ops-triage/events` works end-to-end and a request against a made-up `app_id` with no manifest is rejected with a clear `4xx`.

---

## Week 2 — Orchestration + feedback loop

### Day 6 — `memory-store`: real context, not a stub

**Goal**: a working gRPC service serving real behavioral context out of Redis, backed durably by Postgres.

- Redis schema: keys prefixed by `app_id`/`memory_namespace` (from the App Manifest), then per alert-source/type, recurrence counts and recent-alert aggregates over rolling windows (5m/1h/24h) — the alert-triage analogue of Sentinel's velocity aggregates. Namespacing by app now avoids a data migration when apps #2/#3 land Day 15.
- Postgres `memory_history` table: durable snapshot on every update (rebuild-from-source-of-truth path), also keyed by `app_id`.
- `GetContext` gRPC handler: takes `app_id` in the request, reads Redis, falls back to computing from Postgres on a cache miss and repopulates Redis.
- `backend/scripts/seed.py`: populate Redis + Postgres with synthetic historical alerts.
- **Unit tests**: aggregate window boundary math (edge-of-window cases, empty-history alert types), the cache-miss-falls-back-and-repopulates path.

**Definition of done**: a gRPC test client against `memory-store` returns real, correct aggregates for seeded alert types; `GetContext` latency measured (should be low-single-digit ms).

---

### Day 7 — `orchestrator` uses context; supervisor logic; second agent (root-cause-summarizer)

**Goal**: agent decisions demonstrably change based on retrieved context, low-confidence cases get flagged for a human, and `it-ops-triage` gains a second, callable-only agent that deepens the explanation on escalated cases without changing the triage decision itself.

- gRPC client from `orchestrator` to `memory-store` (connection reuse, not a channel per call).
- Retrieved context (recurrence history, similar past alerts) feeds into the agent's reasoning — decide and document whether this is a direct gRPC call before the tool-calling loop or exposed as another callable tool.
- Lightweight supervisor step: a confidence signal on the agent's decision; below a threshold, the decision is marked `ESCALATED` regardless of what the agent itself concluded.
- Register a second agent in `it-ops-triage`'s App Manifest: `root-cause-summarizer` (role: `callable`), its own `prompt_ref` (write a deeper root-cause narrative, not a triage decision) and `tool_allowlist` (reuses `lookup_runbook`).
- `orchestrator`'s `Agent.RunAgent` gRPC handler dispatches on `agent_id` (empty → entry agent, existing behavior unchanged). On `ESCALATE`, `triage-agent`'s loop calls `RunAgent` again with `agent_id: "root-cause-summarizer"`, gets back `reasons[]` holding the narrative (`decision` left `DECISION_UNSPECIFIED`), and folds those reasons — prefixed `"root-cause-summarizer: ..."` — into its own final `reasons[]` before publishing `alert.decided`. One direction only: the summarizer never calls back, and its own response is never published directly.
- **Unit tests**: confidence-threshold routing (table-driven), mocked `memory-store` client, `agent_id` dispatch routing (empty → entry agent; explicit ID → that agent's handler), reason-folding logic (summarizer's `reasons[]` end up prefixed and appended, never dropped or double-published), delegation only fires on `ESCALATE` (not on `AUTO_RESOLVE`/`SUPPRESS`).

**Definition of done**: the same alert type produces a different decision depending on seeded `memory-store` context (e.g., recurring/known-noise → auto-resolve, novel → escalate); an `ESCALATE`d alert's final `reasons[]` includes at least one `root-cause-summarizer`-prefixed entry; exactly one `alert.decided` event is published per alert regardless of the delegation call.

---

### Day 8 — `review-console`: human-in-the-loop

**Goal**: escalated alerts are reviewable, and analyst verdicts are recorded.

- `review-console`: Kafka consumer on `alert.decided`, persists `ESCALATED` alerts into the `cases` table (auto-resolved/suppressed alerts are not persisted here — high volume, no human action needed). `cases` carries an `app_id` column from the start.
- REST API: `GET /cases` (list, filterable by `app_id` among other fields), `GET /cases/{id}` (detail, including `reasons[]`/tool-call trace — this is where explainability becomes visible to a human), `POST /cases/{id}/verdict`.
- On verdict submission: persist it, produce `verdict.recorded` to Kafka.
- **Unit tests**: verdict state-machine (a case moves `OPEN` → `RESOLVED` exactly once; a verdict on an already-resolved case is rejected with a clear error), persistence filter logic (only `ESCALATED` alerts land in `cases`).

**Definition of done**: an escalated alert appears in `GET /cases` with its reasoning visible; submitting a verdict via `POST /cases/{id}/verdict` produces `verdict.recorded`.

---

### Day 9 — Close the feedback loop

**Goal**: analyst verdicts measurably change future agent behavior.

- Extend `memory-store`'s Kafka consumer to handle `verdict.recorded` — a confirmed-noise verdict should lower future escalation likelihood for that alert type/source; a confirmed-real-incident verdict should raise it.
- Remove the Day 4 throwaway debug endpoint on `ingestion` now that `review-console` is the real read path.
- **Unit tests**: verdict-driven adjustment logic against a mocked/in-memory Redis.

**Definition of done**: re-querying `GetContext` for an alert type after a verdict shows a measurable change in the returned aggregates.

---

### Day 10 — Week 2 integration pass

**Goal**: the full loop works end to end, and drift from the week is caught.

- Manually walk one alert through the entire chain: `ingestion` → `orchestrator` (with `memory-store` context) → `review-console` → verdict → `memory-store` adjustment — observe each stage.
- Fix any schema/contract drift introduced across Days 6–9 (a Day 6 test asserting on something Day 9 quietly changed, etc.).
- **Unit tests**: full regression run across `ingestion`, `orchestrator`, `memory-store`, `review-console`, `registry`, `tool-gateway`.

**Definition of done**: one alert can be traced through the entire feedback loop by hand, and `pytest` is green across every service touched so far.

---

## Week 3 — Platform concerns

### Day 11 — `tool-gateway`: resilience + sandboxing

**Goal**: a broken tool degrades the platform predictably instead of hanging or crashing it.

- Wrap tool execution with circuit-breaker + retry/backoff, adapted from `ai_microservice_patterns/07_circuit_breaker_retry_backoff`.
- Basic sandboxing: enforce a timeout per tool call, validate tool input against the schema registered in `registry` before execution.
- **Unit tests**: circuit breaker opens after N consecutive failures, retry/backoff timing, timeout enforcement.

**Definition of done**: deliberately break a tool (inject a sleep/error), observe the circuit breaker open, and confirm `orchestrator` degrades gracefully (e.g., escalates by default) rather than hanging.

---

### Day 12 — `registry`: versioning + a second tool + second App Manifest

**Goal**: adding a new tool, or a whole new app, requires no code change to `orchestrator`.

- Add a `version` field to registered agents/tools; `orchestrator` resolves "latest" or a pinned version.
- Register a second real tool (e.g., similar-past-incident lookup, backed by `memory-store`) under the existing `it-ops-triage` app.
- Register a second **App Manifest** (`app_id` for whatever app #2 actually is — pick its real shape now) with its own agent, at least one tool, and event schema, proving the Day 5 App Manifest mechanism holds more than one app.
- **Unit tests**: version resolution logic, `registry` rejects a malformed schema on register (both tool and App Manifest registration).

**Definition of done**: the new tool is available to `orchestrator` purely by registering it in `registry` — no `orchestrator` code change, just a restart/refresh of its discovery call; an event tagged with app #2's `app_id` routes to app #2's agent/tools/memory-namespace, not app #1's.

---

### Day 13 — Rate & cost governance

**Goal**: the platform has a visible, enforced budget instead of unlimited LLM/tool spend.

- Per-agent token/cost budget tracking (Redis counter), reject or queue new runs when a budget is exceeded.
- Basic per-alert-source rate limiting on `ingestion`.
- **Unit tests**: budget counter increment/reset logic, rejection behavior when over budget.

**Definition of done**: artificially lower a budget and observe `orchestrator` refuse new agent runs with a clear, explicit reason — not a silent failure or a hang.

---

### Day 14 — Reliability: outbox + DLQ on `ingestion`

**Goal**: no alert is silently lost if Kafka is briefly unavailable.

- Adapt `ai_microservice_patterns/08_outbox_dead_letter_queue` into `ingestion`'s publish path: an outbox table + relay, with a DLQ for repeated publish failures.
- **Unit tests**: outbox write-then-relay round trip, DLQ routing on a forced/injected failure.

**Definition of done**: kill Kafka briefly during a burst of `POST /alerts`, restore it, and confirm every alert is eventually published (outbox drains) with induced failures landing in the DLQ rather than vanishing.

---

### Day 15 — Week 3 integration + stand up apps #2 and #3

**Goal**: the platform-core-vs-domain-adapter boundary is proven, not just asserted — by actually running 2-3 apps side by side.

- Review the codebase: confirm IT-ops-specific code (alert schema, runbook tool, triage prompt) is isolated from platform code (`registry`, `tool-gateway`, `orchestrator`'s agent-loop skeleton, `memory-store`'s generic context API); refactor any leakage found.
- Build out app #2 (registered Day 12) and app #3 as real, minimal adapters: each gets its own module under `backend/apps/{app_id}/` (event schema, prompt, any app-specific tool), registered in `registry` as its own App Manifest — no changes to `orchestrator`, `memory-store`, `tool-gateway`, or `registry` code itself.
- Run all 2-3 apps' events through the shared pipeline concurrently and confirm no cross-app leakage: app #2's and #3's memory context, tool access, and cases stay scoped to their own `app_id`.
- **Unit tests**: full regression run across all registered apps.

**Definition of done**: `docs/ARCHITECTURE.md`'s platform-core-vs-adapter section is checked against the actual code and corrected if it drifted; 2-3 apps are simultaneously registered and each produces correctly-scoped decisions, memory context, and cases through the identical platform code path.

---

## Week 4 — Observability & evals

### Day 16 — Full OTel pipeline

**Goal**: one alert's journey is visible as a single connected trace.

- Point every service's OTel SDK at the real Collector (swap from stdout-only); Collector fans out to Tempo (traces), Mimir (metrics), Loki (logs).
- Propagate trace context through Kafka message headers so a trace spans `ingestion` → `orchestrator` → `tool-gateway` (gRPC) → `review-console`/Celery as one connected trace.
- Grafana dashboards: RED metrics per service, Kafka consumer-lag, one domain dashboard (decision distribution: auto-resolve/escalate/suppress).
- **Unit tests**: trace-context propagation round-trip (a trace ID injected into a Kafka header comes back unchanged on the consumer side).

**Definition of done**: fire one alert through the full system and find it as a single connected span tree in Tempo, with correlated logs in Loki.

---

### Day 17 — LLM-specific observability

**Goal**: cost and token spend are visible per agent call, not invisible.

- Custom OTel span attributes on every LLM call: model, prompt tokens, completion tokens, cost estimate, tool-call count, latency.
- Grafana dashboard: cost/token burn rate over time, tool-call volume by tool.
- **Unit tests**: span-attribute population logic against a mocked LLM response.

**Definition of done**: the dashboard shows real token/cost numbers from a handful of live test alerts run through the system.

---

### Day 18 — Eval harness

**Goal**: a prompt or logic regression is caught automatically, not by eyeballing a demo.

- A small labeled fixture set (synthetic alerts with expected decisions) checked into the repo.
- Eval script: run `orchestrator` against the fixtures, report accuracy/precision on escalate-vs-auto-resolve, flag regressions.
- Wire as a Celery task (batch re-eval trigger), mirroring Sentinel's batch re-scoring shape.
- **Unit tests**: the eval scoring logic itself (given known predictions vs. labels, correct metrics computed).

**Definition of done**: the eval script reports a baseline accuracy number locally; deliberately regressing the prompt/logic causes the eval to catch it.

---

### Day 19 — Load & resilience

**Goal**: the platform's behavior under load and under failure is measured, not assumed.

- Load test (k6/locust) against `POST /alerts` at a sustained rate; capture p50/p95/p99.
- One deliberate failure scenario (kill `memory-store` or `tool-gateway` mid-load), observed through Grafana/Tempo — confirm `orchestrator` degrades predictably (e.g., escalate-by-default) rather than silently misbehaving.
- **Unit tests**: none new — full regression suite run.

**Definition of done**: documented load-test numbers and one documented, observed failure-mode behavior.

---

### Day 20 — Polish, docs, buffer

**Goal**: a stranger (or future-you) can pick this up from a clean checkout.

- Fill in `CLAUDE.md`'s status section with real build/run/test commands.
- README: what it is, how to run it (`docker compose up`, seed script, example `curl`), link to `docs/ARCHITECTURE.md`.
- Buffer time for whichever day ran over — treat Day 20 morning as unscheduled slack, not additional scope.
- **Unit tests**: no new logic — full suite across all 6 services plus Celery as a single regression pass.

**Definition of done**: documented load-test numbers against a stated target, one documented failure-mode behavior, a README that lets a stranger run the whole stack from a clean checkout, and a full `pytest` run passing green.

---

## Explicitly out of scope for this 4-week build (phase-two candidates)

- Kubernetes manifests / Helm charts (Compose is the target for this build).
- Agent-to-agent (A2A) communication *across apps* — apps registered via the App Manifest (Day 5) are isolated tenants sharing platform infrastructure (Kafka/gRPC/REST/MCP), not agents calling each other directly. A dedicated A2A protocol is validation work for later. (Within-app delegation from an entry agent to a callable-only agent, e.g. `triage-agent → root-cause-summarizer` on Day 7, is in scope — that's a fixed edge in one app's own manifest, not agent discovery.)
- A dynamic/self-serve app-registration UI or workflow — apps #2-3 are registered by hand (Day 12, Day 15), not through a built admin flow.
- A real trained classifier replacing/augmenting the agent's reasoning.
- Auth/authz on the analyst (`review-console`) API.
- A frontend (this build stays API + Grafana only).
