# Sequence diagrams

Four representative runtime flows through the platform, drawn from
`docs/ARCHITECTURE.md` §3–§9. The first three aren't formally named "use
cases" in that doc — they're the sequences that are actually distinct
end-to-end (the fourth possible decision path, `SUPPRESS`, is
sequence-identical to `AUTO_RESOLVE` and is folded into diagram 1). Each
diagram is followed by a services/messages/transport table for quick
reference. Section numbers point back to the source of truth.

Diagram 4 uses a second app, `cost-anomaly-triage`, to make multi-app
isolation visible. It's documented as a second worked example in
`docs/ARCHITECTURE.md` §3, alongside `it-ops-triage` — but **no code for it
exists in the repo**, and it isn't scheduled in `docs/plan.md` (which only
tracks unnamed "apps #2-3" at Day 12/15). Treat both the §3 description and
this diagram as illustrative of the multi-app model, not documentation of
something already built.

---

## 1. Auto-resolve / suppress — no human involved

The common-case path: `orchestrator` decides without escalating, so
`review-console` receives the event but never persists a case (§4 steps 1–3).

```mermaid
sequenceDiagram
    participant Client as external client
    participant Ingestion as ingestion
    participant Orchestrator as orchestrator
    participant Registry as registry
    participant MemoryStore as memory-store
    participant ToolGateway as tool-gateway
    participant ReviewConsole as review-console

    Client->>Ingestion: POST /apps/{app_id}/events (REST)
    Ingestion->>Ingestion: validate against event_schema_ref
    Ingestion-->>Client: 202 Accepted
    Ingestion->>Orchestrator: alert.received (Kafka · RunAgentRequest)

    Orchestrator->>Registry: resolve app_id → manifest (REST, 30s TTL cache)
    Registry-->>Orchestrator: App Manifest

    Orchestrator->>MemoryStore: GetContext (gRPC)
    MemoryStore-->>Orchestrator: GetContextResponse (windowed aggregates)

    Orchestrator->>ToolGateway: tool-call (MCP, allowlisted tools only)
    ToolGateway-->>Orchestrator: tool-result

    Note over Orchestrator: supervisor/confidence logic →<br/>decision = AUTO_RESOLVE or SUPPRESS

    Orchestrator->>ReviewConsole: alert.decided (Kafka · RunAgentResponse)
    Note over ReviewConsole: decision ≠ ESCALATE →<br/>not persisted to `cases`, no analyst action
```

| Step | Services | Message | Transport |
|---|---|---|---|
| 1 | client → `ingestion` | event payload | REST |
| 2 | `ingestion` → `orchestrator` | `alert.received` (`RunAgentRequest`) | Kafka |
| 3 | `orchestrator` → `registry` | manifest read | REST |
| 4 | `orchestrator` → `memory-store` | `GetContext` | gRPC |
| 5 | `orchestrator` → `tool-gateway` | tool-call / tool-result | MCP |
| 6 | `orchestrator` → `review-console` | `alert.decided` (`RunAgentResponse`) | Kafka |

---

## 2. Escalate — delegation + human review + verdict feedback

The full loop: entry agent delegates to the callable agent, a case lands in
front of an analyst, and the verdict flows back to close the loop (§3 agent
delegation, §5 delegation call, §4 steps 4–5).

```mermaid
sequenceDiagram
    participant Client as external client
    participant Ingestion as ingestion
    participant Orchestrator as orchestrator (triage-agent)
    participant Registry as registry
    participant MemoryStore as memory-store
    participant ToolGateway as tool-gateway
    participant ReviewConsole as review-console
    participant Analyst as analyst (human)

    Client->>Ingestion: POST /apps/{app_id}/events (REST)
    Ingestion->>Ingestion: validate against event_schema_ref
    Ingestion-->>Client: 202 Accepted
    Ingestion->>Orchestrator: alert.received (Kafka · RunAgentRequest)

    Orchestrator->>Registry: resolve app_id → manifest (REST, 30s TTL cache)
    Registry-->>Orchestrator: App Manifest

    Orchestrator->>MemoryStore: GetContext (gRPC)
    MemoryStore-->>Orchestrator: GetContextResponse

    Orchestrator->>ToolGateway: tool-call (MCP, allowlisted tools only)
    ToolGateway-->>Orchestrator: tool-result

    Note over Orchestrator: supervisor/confidence logic →<br/>decision leans ESCALATE

    Orchestrator->>Orchestrator: RunAgent (gRPC, self-call · agent_id=root-cause-summarizer)
    Orchestrator->>ToolGateway: tool-call, summarizer's own allowlist (MCP)
    ToolGateway-->>Orchestrator: tool-result
    Orchestrator->>Orchestrator: reasons[] (root-cause narrative) returned to triage-agent
    Note over Orchestrator: triage-agent folds summarizer's reasons[]<br/>into its own, prefixed "root-cause-summarizer: ..."

    Orchestrator->>ReviewConsole: alert.decided (Kafka · RunAgentResponse, decision=ESCALATE)
    Note over ReviewConsole: persists `cases` row (Postgres, app_id-scoped)

    Analyst->>ReviewConsole: GET case list/detail (REST)
    ReviewConsole-->>Analyst: case incl. reasons[] (explainability trace)
    Analyst->>ReviewConsole: submit verdict (REST)

    ReviewConsole->>MemoryStore: verdict.recorded (Kafka · {app_id, case_id, alert_key, verdict, verdict_by, recorded_at})
    MemoryStore->>MemoryStore: update Redis ctx + Postgres memory_history<br/>for that alert_key (closes feedback loop)
```

| Step | Services | Message | Transport |
|---|---|---|---|
| 1 | client → `ingestion` | event payload | REST |
| 2 | `ingestion` → `orchestrator` | `alert.received` (`RunAgentRequest`) | Kafka |
| 3 | `orchestrator` → `registry` | manifest read | REST |
| 4 | `orchestrator` → `memory-store` | `GetContext` | gRPC |
| 5 | `orchestrator` → `tool-gateway` | tool-call / tool-result (triage-agent) | MCP |
| 6 | `orchestrator` → `orchestrator` | `RunAgent` self-call, `agent_id=root-cause-summarizer` | gRPC |
| 7 | `orchestrator` → `tool-gateway` | tool-call / tool-result (summarizer) | MCP |
| 8 | `orchestrator` → `review-console` | `alert.decided` (`RunAgentResponse`, ESCALATE) | Kafka |
| 9 | analyst → `review-console` | case read / verdict submit | REST |
| 10 | `review-console` → `memory-store` | `verdict.recorded` | Kafka |

---

## 3. Manifest edit — no redeploy, takes effect within one TTL window

Demonstrates the "config, not code" promise: an operator disables a tool via
`registry`, and the next alert for that app picks up the change without an
`orchestrator` restart (§3: "this is what `plan.md` Day 5's 'no code change
or redeploy' definition of done depends on").

```mermaid
sequenceDiagram
    participant Operator as operator (human)
    participant Registry as registry
    participant Orchestrator as orchestrator
    participant ToolGateway as tool-gateway

    Operator->>Registry: update App Manifest — remove tool from tool_allowlist (REST)
    Registry->>Registry: persist to Postgres `apps.manifest` (jsonb)
    Registry-->>Operator: 200 OK

    Note over Orchestrator: in-memory manifest cache still holds<br/>the old manifest, TTL default 30s

    Note over Orchestrator,Registry: ... next alert.received arrives,<br/>cache entry has expired ...

    Orchestrator->>Registry: resolve app_id → manifest (REST, cache miss)
    Registry-->>Orchestrator: updated App Manifest (tool no longer allowlisted)

    Orchestrator->>ToolGateway: tool-call loop (MCP) — disabled tool never invoked
    Note over Orchestrator,ToolGateway: change took effect with no orchestrator<br/>restart or redeploy
```

| Step | Services | Message | Transport |
|---|---|---|---|
| 1 | operator → `registry` | manifest update | REST |
| 2 | `registry` → Postgres | persist `apps.manifest` (jsonb) | internal (Postgres write) |
| 3 | `orchestrator` → `registry` | manifest read, post-TTL-expiry | REST |
| 4 | `orchestrator` → `tool-gateway` | tool-call (now minus the disabled tool) | MCP |

---

## 4. Two apps, one shared core — isolation in practice

*Illustrative — `cost-anomaly-triage` is invented for this diagram; only
`it-ops-triage` actually exists in the repo.* Same `orchestrator`,
`tool-gateway`, `memory-store` process handling both apps' alerts back to
back, showing where the §3 isolation guarantee (`app_id`, `memory_namespace`,
`tool_allowlist`) actually bites.

```mermaid
sequenceDiagram
    participant ClientA as client (it-ops-triage)
    participant ClientB as client (cost-anomaly-triage)
    participant Ingestion as ingestion
    participant Orchestrator as orchestrator (shared)
    participant Registry as registry
    participant MemoryStore as memory-store (shared)
    participant ToolGateway as tool-gateway (shared)
    participant ReviewConsole as review-console

    ClientA->>Ingestion: POST /apps/it-ops-triage/events (REST)
    Ingestion->>Orchestrator: alert.received (Kafka · app_id=it-ops-triage)
    Orchestrator->>Registry: resolve app_id=it-ops-triage → manifest (REST)
    Registry-->>Orchestrator: Manifest A — agent: triage-agent<br/>tools: runbook-search, page-oncall<br/>memory_namespace="it-ops-triage:"
    Orchestrator->>MemoryStore: GetContext (gRPC · app_id=it-ops-triage)
    Note over MemoryStore: key = ctx:it-ops-triage:{alert_key}:{window}
    MemoryStore-->>Orchestrator: GetContextResponse
    Orchestrator->>ToolGateway: tool-call (MCP · allowlist = runbook-search, page-oncall)
    ToolGateway-->>Orchestrator: tool-result
    Orchestrator->>ReviewConsole: alert.decided (Kafka · app_id=it-ops-triage)
    Note over ReviewConsole: `cases` row written with app_id=it-ops-triage

    ClientB->>Ingestion: POST /apps/cost-anomaly-triage/events (REST)
    Ingestion->>Orchestrator: alert.received (Kafka · app_id=cost-anomaly-triage)
    Orchestrator->>Registry: resolve app_id=cost-anomaly-triage → manifest (REST)
    Registry-->>Orchestrator: Manifest B — agent: cost-triage-agent<br/>tools: billing-lookup, similar-past-case-lookup<br/>memory_namespace="cost-anomaly-triage:"
    Orchestrator->>MemoryStore: GetContext (gRPC · app_id=cost-anomaly-triage)
    Note over MemoryStore: key = ctx:cost-anomaly-triage:{alert_key}:{window}<br/>— same alert_key as app A's request, never collides
    MemoryStore-->>Orchestrator: GetContextResponse
    Orchestrator->>ToolGateway: tool-call (MCP · allowlist = billing-lookup, similar-past-case-lookup)
    Note over ToolGateway: cost-triage-agent cannot see or call<br/>runbook-search / page-oncall — app A's tools, not allowlisted here
    ToolGateway-->>Orchestrator: tool-result
    Orchestrator->>ReviewConsole: alert.decided (Kafka · app_id=cost-anomaly-triage)
    Note over ReviewConsole: `cases` row written with app_id=cost-anomaly-triage<br/>— every read filters by app_id, rows never cross apps
```

| Step | Services | Message | Transport | Isolation mechanism |
|---|---|---|---|---|
| 1–2 | client A → `ingestion` → `orchestrator` | event / `alert.received` (`app_id=it-ops-triage`) | REST / Kafka | `app_id` carried on every payload |
| 3 | `orchestrator` → `registry` | manifest read | REST | manifest lookup keyed by `app_id` |
| 4 | `orchestrator` → `memory-store` | `GetContext` | gRPC | `memory_namespace` prefixes the Redis key |
| 5 | `orchestrator` → `tool-gateway` | tool-call | MCP | `tool_allowlist` scopes which tools this app's agent can reach |
| 6 | `orchestrator` → `review-console` | `alert.decided` | Kafka | `cases` row carries `app_id`, filtered on every read |
| 7–12 | same steps, client B, `app_id=cost-anomaly-triage` | — | — | identical mechanisms, disjoint values — same `alert_key` on both sides never collides |

`similar-past-case-lookup` (from the §3 worked example, `scope: global`)
would be available to both apps' allowlists if each manifest chose to
include it — `billing-lookup` and `runbook-search`/`page-oncall` are
`scope: app` and can never appear outside the manifest that declares them.
