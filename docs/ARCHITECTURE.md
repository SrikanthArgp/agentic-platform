# Architecture

Status: living design doc. `docs/plan.md` is the day-by-day build sequence that
implements this; when the two disagree, treat it as drift and fix whichever
is wrong (`plan.md` Day 15 explicitly checks §3/§4 against the code).

Section numbers below are referenced from code comments (`backend/proto/*.proto`)
— keep them stable; add new material as new top-level sections rather than
renumbering.

---

## 1. Overview

An agentic microservices platform for building event-triggered agents that
decide, explain, and improve from feedback. The platform core is generic
(routing, tool-calling, memory, human review, observability); everything
domain-specific is configuration, not code.

The reference build runs 2-3 **apps** (configurable use cases / products) on
one shared core:

1. **IT-ops alert triage** (app #1, built Weeks 1-2) — `ingestion` accepts
   alerts; the agent decides `AUTO_RESOLVE` / `ESCALATE` / `SUPPRESS`.
2. **App #2** (registered Day 12, stood up Day 15) — shape TBD when reached,
   registered the same way app #1 is.
3. **App #3** (stood up Day 15) — same.

Each app is an isolated tenant of the same infrastructure: its own agent(s),
tools, event schema, and memory namespace, declared once as an **App
Manifest** in `registry` (§3). No app's code lives inside another's, and no
platform service branches on `app_id` in its logic — only in what data it
reads.

---

## 2. Design principles

- **Platform core vs. domain adapter is a directory boundary, not a doc
  section.** Platform code (`registry`, `tool-gateway`, `orchestrator`'s
  agent-loop, `memory-store`'s context API) never imports anything from
  `backend/apps/*`. Domain adapters import platform contracts, never the
  reverse.
- **App identity is explicit, everywhere.** Every event envelope, gRPC
  request, and app-scoped row carries `app_id`. Routing to the right agent,
  tool set, or memory namespace is always a `registry` lookup, never
  inferred from payload shape.
- **Contracts before implementation.** `.proto` files in `backend/proto/`
  are written before the service code that implements them.
- **Explainability is a first-class field, not a log line.** Every decision
  carries `reasons[]` — populated from the agent's actual tool-call trace,
  not written after the fact.
- **Degrade predictably, never hang or silently misbehave.** A broken tool,
  an exhausted budget, or an unavailable dependency produces an explicit,
  observable outcome (e.g. escalate-by-default), covered in §10.
- **A2A is explicitly excluded.** Apps do not call each other's agents.
  Cross-app "communication," if it's ever needed, would go through the same
  Kafka backbone every app already uses — not a dedicated agent-to-agent
  protocol. See §11.
- **The platform triages; it doesn't detect.** Every app's agent reasons
  over an event some *external* system already flagged as worth looking at
  — an infra monitoring tool posting an infra alert, a cloud cost-anomaly
  tool posting a spend-spike alert, etc. `ingestion`'s REST endpoint is the
  boundary: nothing upstream of it (raw metrics, billing data, log streams)
  is this platform's concern, and no service here runs the
  statistical/threshold logic that decides "this is anomalous" in the first
  place. What the agent does downstream of that — pulling more detail via
  tools, checking `memory-store` for whether this exact pattern has fired
  before, deciding `AUTO_RESOLVE` / `ESCALATE` / `SUPPRESS` — is triage, not
  detection, even though both involve "checking" something.

---

## 3. Multi-app model: the App Manifest

`registry` stores one **App Manifest** per app — the single source of truth
for what an app is allowed to do:

```
App {
  app_id: string                # stable slug, e.g. "it-ops-triage"
  display_name: string

  agents: [{
    agent_id: string
    version: string
    role: "entry" | "callable"  # exactly one "entry" agent per app — the
                                 # one alert.received triggers. "callable"
                                 # agents are invoked only by another agent
                                 # in the same app (§5), never by Kafka.
    prompt_ref: string          # points at this app's prompt template
    tool_allowlist: [string]    # tool_ids this agent may call
  }]

  tools: [{
    tool_id: string
    version: string
    scope: "app" | "global"     # "global" tools (e.g. similar-past-case
                                 # lookup) are available to every app that
                                 # allowlists them; "app" tools belong to
                                 # exactly one app
  }]

  event_schema_ref: string      # validates this app's Kafka payload shape
  memory_namespace: string      # Redis/Postgres key prefix in memory-store
}
```

**Where app-specific code lives**: `backend/apps/{app_id}/` — event schema,
prompt template, and any app-owned tool implementation. `tool-gateway` loads
app-scoped tool implementations from there at startup, keyed by the
`tool_id`s each app's manifest declares; it never hardcodes a tool list.

**Resolution flow**: `orchestrator` resolves `app_id → App Manifest` via a
`registry` REST call, cached in-memory with a short TTL (default 30s) so a
manifest edit (e.g. disabling a tool) takes effect on the next fetch without
a restart — this is what `plan.md` Day 5's "no code change or redeploy"
definition of done depends on.

**Isolation guarantee**: two apps' identical `alert_key` values never
collide. `memory_namespace` prefixes every `memory-store` key; `cases` rows
carry `app_id`; `tool_allowlist` prevents one app's agent from ever seeing
another app's tools.

**What this is not**: apps do not discover or call each other through
`registry`. The manifest is read by platform services to configure a single
app's run, not to wire apps together (§11).

**Agent delegation, within one app**: `it-ops-triage`'s manifest declares
two agents — `triage-agent` (role: `entry`) and `root-cause-summarizer`
(role: `callable`). `triage-agent` can call `root-cause-summarizer`
synchronously via a normal `RunAgent` gRPC call against `orchestrator`'s own
`Agent` service, with `agent_id` set explicitly (§5) — same service, same
contract, dispatched internally by `agent_id`. This is a fixed, declared
edge, one direction only (entry → callable, never the reverse, and a
callable agent never calls another callable agent) — deliberately not agent
discovery or a mesh. Cross-app agent calls remain excluded under A2A (§11);
this is scoped to one app's own manifest.

**A second app, for scale** (`cost-anomaly-triage` — illustrative, not yet
scheduled in `docs/plan.md`; shows the manifest model holding a second,
unrelated domain on the same core rather than describing something already
built). Trigger is a cloud cost-anomaly alert instead of an infra alert.
`cost-triage-agent` (role: `entry`) decides `AUTO_RESOLVE` / `ESCALATE` /
`SUPPRESS` on a spend spike using an app-owned `billing-lookup` tool
(per-service/resource/tag cost breakdown for the flagged window) plus the
same global `similar-past-case-lookup` tool `it-ops-triage` uses — global
tools are exactly the pattern this field exists to support: one
implementation, allowlisted by whichever apps need it.
`memory-store.GetContext`, keyed under this app's own `memory_namespace`,
lets a recurring known spike (e.g. a monthly batch job) get `SUPPRESS`ed
instead of re-escalated every cycle. On `ESCALATE`, it optionally delegates
to a callable `cost-root-cause-agent` — the same fixed entry→callable edge
as `root-cause-summarizer` above — to correlate the spike against recent
deploys/config changes before an analyst sees it in `review-console`.

This app's `alert_key` values (e.g. `service:region`) can collide textually
with `it-ops-triage`'s `alert_key` values without any actual collision — the
isolation guarantee above (`memory_namespace` prefix, `app_id`-scoped
`cases` rows, disjoint `tool_allowlist`s) is exactly what keeps them apart
on the same shared `orchestrator`/`tool-gateway`/`memory-store` instances.

---

## 4. Service map & data flow

```
                         REST                                   REST
  external client ─────────────────▶ ingestion            registry ◀── (manifest reads)
                                         │                     ▲
                                         │ Kafka                     │ REST (resolve app_id)
                                         │ alert.received             │
                                         ▼                     │
                                    orchestrator ────────────────┘
                                     │        │
                          MCP (tools)│        │ gRPC (GetContext)
                                     ▼        ▼
                              tool-gateway  memory-store ◀──── Postgres
                                     │        ▲                 (memory_history)
                                     │        │ Kafka
                                     │        │ verdict.recorded
                                     │        │
                                     ▼        │
                                  (result)  review-console ◀── REST (analyst)
                                     │        ▲
                                     │ Kafka  │ Kafka
                                     │ alert.decided
                                     └────────┘
```

Steps, app-agnostic:

1. `POST /apps/{app_id}/events` (`ingestion`) validates against that app's
   `event_schema_ref`, publishes `alert.received` (payload = `RunAgentRequest`,
   protobuf-encoded — §5), returns `202` immediately.
2. `orchestrator` consumes `alert.received`, resolves the app's manifest
   (§3), calls `memory-store.GetContext` (gRPC, §6) for behavioral context,
   runs its tool-calling loop against `tool-gateway` (MCP, allowlisted tools
   only), and applies supervisor/confidence logic. If the app declares a
   callable-only agent and the decision is `ESCALATE`, the entry agent
   delegates to it here (§3/§5) — an internal `RunAgent` gRPC call, not a
   new hop in this diagram.
3. `orchestrator` publishes `alert.decided` (payload = `RunAgentResponse`).
4. `review-console` consumes `alert.decided`; only `ESCALATED` decisions are
   persisted into `cases` (app-scoped). An analyst reviews via REST and
   submits a verdict, publishing `verdict.recorded`.
5. `memory-store` consumes `verdict.recorded` and adjusts that app's
   aggregates for that `alert_key`, closing the feedback loop.

Celery workers (batch re-eval, outbox relay) sit off to the side of this
hot path — triggered on a schedule or by failure conditions, not by every
event.

---

## 5. Agent decision contract — `RunAgent`

Defined in `backend/proto/agent.proto`. This is both the `orchestrator`
gRPC entrypoint and the schema for the `alert.received`/`alert.decided`
Kafka payloads — one contract, two transports, so there's no second schema
to keep in sync.

- **Request** (`RunAgentRequest`): `app_id`, `agent_id` (empty → the app's
  entry agent; set explicitly to call a callable-only agent, §3), `alert_id`,
  `alert_key`, `source`, `severity`, `message`, `timestamp_unix_ms`.
- **Response** (`RunAgentResponse`): `app_id`, `agent_id` (echoes which
  agent produced this response), `alert_id`, `decision` (`AUTO_RESOLVE` /
  `ESCALATE` / `SUPPRESS`, left `DECISION_UNSPECIFIED` for a callable-only
  agent — it explains, it doesn't classify), `reasons[]` (explainability —
  every signal that drove the decision), `tool_calls[]` (summary of each
  tool invocation: `tool_name`, `result_summary`).

`reasons[]` is populated from the agent's actual tool-call trace and
supervisor logic, not written after the fact — this is the platform's
explainability contract, and it's what `review-console` (§4 step 4) shows an
analyst.

**Delegation call** (`it-ops-triage` only, for now): on `ESCALATE`,
`triage-agent` calls `root-cause-summarizer` (§3) with `agent_id` set,
gets back `reasons[]` holding a deeper root-cause narrative, and folds them
into its own final `reasons[]` — prefixed `"root-cause-summarizer: ..."` —
before publishing `alert.decided`. The summarizer's own `RunAgentResponse`
is never published directly; exactly one `alert.decided` event goes out per
alert, from the entry agent. Gated to `ESCALATE` only, since that's where a
human is actually going to read the explanation (§4 step 4) — auto-resolved
and suppressed alerts don't pay for the extra LLM call.

---

## 6. Memory context contract — `GetContext`

Defined in `backend/proto/memory_store.proto`. Synchronous gRPC only — no
Kafka topic, since `orchestrator` needs this inline before it can reason.

- **Request** (`GetContextRequest`): `app_id`, `alert_key`.
- **Response** (`GetContextResponse`): `app_id`, `alert_key`, aggregates
  over `window_1h` / `window_24h` / `window_7d` (`alert_count`,
  `escalation_count`, `suppression_count` each), `is_novel_alert`,
  `has_confirmed_incident_history`.

Backed by Redis (hot path, keyed by `{memory_namespace}:{alert_key}:{window}`)
with Postgres `memory_history` as the durable source of truth — a Redis miss
recomputes from Postgres and repopulates Redis (§8).

---

## 7. Observability & tracing

Every service ships OpenTelemetry SDK from the day it's created (logs to
stdout until Day 16 wires the real Collector — Tempo/Mimir/Loki via
Grafana). Trace context propagates through Kafka message headers, so one
event's journey (`ingestion → orchestrator → tool-gateway/memory-store →
review-console`) is a single connected span tree, not five disjoint traces.

The full tool-call **transcript** (every tool call, its arguments, and raw
result — not just the `result_summary` carried in `RunAgentResponse.tool_calls`)
lives in the trace, not in `cases` or Kafka. `cases`/`RunAgentResponse` carry
the *explainability summary*; the trace carries the *forensic detail*, kept
separate so the hot-path contract stays small.

LLM-specific spans (Day 17) attach `model`, prompt/completion tokens, cost
estimate, and tool-call count to every agent call — this is what per-app
cost governance (§10) and the cost/token Grafana dashboard read from.

---

## 8. Storage schemas

**Postgres** (single local instance, `backend/local/postgres/init.sql`):

| Table | Key columns | Notes |
|---|---|---|
| `cases` | `id`, `app_id`, `alert_id`, `alert_key`, `decision`, `reasons` (jsonb), `status` (`OPEN`\|`RESOLVED`), `verdict`, `created_at`, `resolved_at` | Only `ESCALATED` decisions land here (§4 step 4). `app_id` filter on every read. |
| `memory_history` | `id`, `app_id`, `alert_key`, `window`, snapshot fields, `recorded_at` | Durable snapshot on every `memory-store` update; Redis's rebuild-from-source-of-truth path (§6). |
| `apps` | `app_id` (PK), `display_name`, `manifest` (jsonb), `created_at`, `updated_at` | `registry`'s App Manifest store (§3) — one row per app, manifest kept as a single jsonb document rather than normalized, since it's read whole and written rarely. |

**Redis** (`memory-store`, `orchestrator`):

| Key pattern | Purpose |
|---|---|
| `ctx:{memory_namespace}:{alert_key}:{window}` | Rolling aggregate counts (§6), TTL'd per window. |
| `budget:{app_id}:{agent_id}:{date}` | Per-agent token/cost counters (Day 13, §10). |

---

## 9. Transport summary

| Link | Protocol | Payload |
|---|---|---|
| external client → `ingestion` | REST | app-specific JSON, validated against `event_schema_ref` |
| `ingestion` → `orchestrator` | Kafka (`alert.received`) | `RunAgentRequest` (protobuf) |
| `orchestrator` → `tool-gateway` | MCP | tool-call / tool-result |
| `orchestrator` → `memory-store` | gRPC (`GetContext`) | `GetContextRequest`/`Response` (protobuf) |
| `orchestrator` → `registry` | REST | App Manifest / tool schema reads |
| `orchestrator` → `review-console` | Kafka (`alert.decided`) | `RunAgentResponse` (protobuf) |
| `review-console` → `memory-store` | Kafka (`verdict.recorded`) | `{app_id, case_id, alert_key, verdict, verdict_by, recorded_at}` (JSON — no gRPC contract needed, one-directional feed) |
| external client → `review-console` | REST | case list/detail, verdict submission |
| entry agent → callable agent (within one app) | gRPC (`RunAgent`, self-call) | `RunAgentRequest`/`Response` (protobuf) — `orchestrator` re-entering its own `Agent` service with `agent_id` set (§5), not a distinct service link |

No agent-to-agent link exists or is planned for this build (§11) — the row
above is intra-app delegation inside a single `orchestrator` process, not a
cross-service or cross-app link.

---

## 10. Reliability & governance

- **Tool resilience** (Day 11): circuit breaker + retry/backoff around every
  `tool-gateway` call; a tripped breaker makes `orchestrator` degrade to a
  safe default (escalate) rather than hang.
- **Cost governance** (Day 13): per-`app_id`/`agent_id` token/cost budget in
  Redis; over-budget runs are rejected with an explicit reason, never
  silently dropped or queued indefinitely.
- **Ingestion reliability** (Day 14): outbox + DLQ on `ingestion`'s Kafka
  publish path — a Kafka outage doesn't lose alerts, and repeated publish
  failures land in a DLQ instead of vanishing.

All three are app-agnostic platform mechanics — no per-app configuration
beyond the `app_id` that budgets and traces are already keyed by.

---

## 11. Out of scope / non-goals

- **Agent-to-agent (A2A) communication *across apps*.** Apps are isolated
  tenants sharing platform infrastructure (Kafka/gRPC/REST/MCP per §9), not
  a mesh of agents calling each other. If cross-app coordination is ever
  needed, the default answer is "publish an event another app's `ingestion`
  can consume," not a new direct-call protocol — and even that is unbuilt
  today. This does *not* cover the within-app entry→callable delegation
  edge in §3/§5 (e.g. `triage-agent → root-cause-summarizer`) — that's a
  fixed, declared call inside one app's own manifest, not agent discovery.
- **Dynamic/self-serve app registration.** Apps #2-3 are registered by hand
  against `registry`'s REST API (`plan.md` Day 12, Day 15), not through a
  built admin UI or workflow.
- **Anomaly/incident detection itself** (§2). Deciding that a metric,
  cost, or log pattern is worth alerting on at all — thresholding,
  statistical baselines, whatever a monitoring tool does before it fires —
  happens upstream of `ingestion` and is never built here. This platform
  only triages events something else already decided to raise.
- Kubernetes/Helm (Compose is the deployment target for this build).
- A real trained classifier augmenting/replacing the agent's reasoning.
- Auth/authz on the `review-console` analyst API.
- A frontend — this build stays API + Grafana only.
