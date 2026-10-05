# Architecture Decision Records

Each ADR records one significant design decision: the context that forced
it, what was decided, the alternatives that were rejected and why, and the
consequences we accept. `docs/ARCHITECTURE.md` describes *what* the system
is; these describe *why* it is that way, and what would make us revisit it.

Rules:

- One decision per file, numbered in order, never renumbered.
- An accepted ADR is not edited to change its decision. If the decision
  changes, write a new ADR and mark the old one `Superseded by ADR-NNNN`.
- Status is one of `Proposed`, `Accepted`, `Superseded`, `Rejected`.
- Upcoming ADRs (identity, data classification, model gateway, ITSM
  integration, credentials, org tenancy) are listed per area in
  `docs/ENTERPRISE_READINESS.md`; they're written when each decision is
  actually made.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-protocol-per-purpose.md) | Protocol per purpose: REST, gRPC, MCP, Kafka | Accepted |
| [0002](0002-kafka-event-backbone.md) | Kafka as the event backbone, keyed by `{app_id}:{alert_key}` | Accepted |
| [0003](0003-apps-as-configuration.md) | Apps are configuration on a shared runtime; a tenant is an app | Accepted |
| [0004](0004-triage-only-read-only-tools.md) | Triage only: no detection, no remediation, read-only tools | Accepted |
| [0005](0005-deterministic-delegation.md) | One-level, manifest-declared agent delegation (`invoke_on`) | Accepted |
| [0006](0006-manifest-in-git.md) | App Manifest source of truth in git, registered by script | Accepted |
| [0007](0007-event-envelope-and-payload.md) | Generic envelope + `Struct` payload; `alert_key` built by `ingestion` | Accepted |
| [0008](0008-outbox-with-celery-relay.md) | Transactional outbox with a Celery relay, Redis as broker | Accepted |
| [0009](0009-fixture-backed-tools.md) | Fixture-backed tools in this build; real connectors are phase two | Accepted |
| [0010](0010-escalation-guardrails.md) | Deterministic `escalate_when` guardrails against prompt injection | Accepted |
| [0011](0011-resolution-notes-on-cases.md) | Fixes recorded on cases (`resolution_notes`), not in memory | Accepted |
| [0012](0012-callable-dispatch-in-process.md) | Run callable agents in-process instead of a gRPC self-call | Proposed |
| [0013](0013-tool-gateway-hot-reload.md) | Hot-reload app-scoped tools in `tool-gateway` via explicit, atomic reload | Rejected (see 0014) |
| [0014](0014-kubernetes-rolling-rollouts.md) | Local Kubernetes from app #3 on; new apps ship by rolling update | Accepted |
| [0015](0015-llm-provider-openai.md) | OpenAI (`gpt-5.4-mini` default) behind a provider-agnostic LLM interface | Accepted |
| [0016](0016-memory-fed-by-alert-decided.md) | `memory-store` counts decisions by consuming `alert.decided`; `RunAgentResponse` carries `alert_key` | Accepted |
| [0017](0017-memory-as-event-log.md) | Memory stored as an event log; rolling windows counted exactly | Accepted |
| [0018](0018-memory-namespace-from-registry.md) | `memory-store` resolves `memory_namespace` from `registry` | Accepted |
| [0019](0019-context-fetched-before-the-loop.md) | `orchestrator` fetches memory context before the agent loop, not as a tool | Accepted |
| [0020](0020-supervisor-confidence.md) | Supervisor confidence = the agent's own estimate, capped by fixed rules | Accepted |
| [0021](0021-guardrails-read-the-envelope.md) | `escalate_when` rules can read the alert envelope (`alert.*`) | Accepted |
| [0022](0022-langgraph-agent-framework.md) | LangGraph for `orchestrator`'s control flow; LangChain's agent inside it | Accepted |
