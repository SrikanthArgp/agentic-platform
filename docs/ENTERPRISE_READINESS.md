# Enterprise Readiness

Status: design position, not scheduled work. This build (`docs/plan.md`)
is a local, synthetic-data reference platform. This doc describes how the
same design extends to an enterprise deployment — identity, data
governance, model strategy, integrations, multi-tenancy, and operations —
and what has to change to get there.

How it relates to other docs:

- `docs/ARCHITECTURE.md` is the system of record for what's built.
  §11 lists what's out of scope; §13 lists the security threats, several
  marked "phase two". This doc is the plan for those phase-two items.
- Each area below ends with the **ADR to write** when its decision is
  actually made. Nothing here is an accepted decision until that ADR
  exists in `docs/adr/`.

Guiding constraint: every change here should be **additive at the
platform boundary** — new adapters, new manifest fields, new consumers —
without changing the agent loop, the decision contract (§5), or the
"apps are configuration" model (ADR-0003). Where an item would break that,
it says so.

---

## 1. Identity and access

### Today

No authentication anywhere: `ingestion` accepts events from anyone,
`review-console` accepts verdicts from anyone (`verdict_by` is
self-asserted), `registry` accepts manifest changes from anyone, and
services trust each other on the local network (§13 T6, T7, T12).

### Target

**Humans — SSO + role-based access, scoped by app**

- Analysts sign in through the company identity provider (OIDC/SAML) at
  `review-console` and `registry`; tokens are validated at each service.
- Roles, each scoped to one or more `app_id`s:

| Role | Can |
|---|---|
| `analyst` | Read cases and submit verdicts for their apps |
| `app-owner` | Everything `analyst` can, plus change their app's manifest, prompts, guardrails |
| `platform-admin` | Platform configuration, budgets, rate limits; not case contents by default |
| `auditor` | Read-only access to cases, decisions, and the audit log |

- `verdict_by` is taken from the authenticated identity, never the request
  body — closes T7's forged-verdict path.

**Machines — workload identity**

- Each alert source gets a credential bound to exactly one `app_id`
  (API key or mTLS client certificate). `ingestion` rejects a source
  posting to any other app (T6).
- Service-to-service: mTLS with workload identities (e.g. SPIFFE-style
  IDs issued per service); Kafka ACLs so each service can only produce to
  and consume from its own topics (T12).
- `register_app.py` runs from CI with a workload identity, not from a
  laptop; `registry` accepts manifest writes only from that identity.

**Audit trail**

An append-only audit log of every security-relevant event: manifest
registrations (who, diff, when), verdicts, guardrail hits, budget
rejections, role changes. `alert.decided` already records every decision;
the audit log adds who-changed-what. Stored separately from operational
tables, with its own retention and access (auditor role only).

### Changes to the platform

- New: auth middleware in each REST service, identity propagated in
  request context and Kafka headers, an `audit_log` table (or topic).
- Manifest gains `owners: [group]` (who can change it) — additive.
- No change to the agent loop or contracts.

**ADR to write**: identity provider integration and role model.

---

## 2. Data governance

### Today

Synthetic data only. Full alert payloads go into every prompt (to an
external LLM provider) and into traces (§13 T10, T11). No retention
policies.

### Target

**Classify and minimize what reaches the model**

- Each app's event schema marks fields with a classification
  (`public`, `internal`, `pii`, `secret`).
- Manifest gains `prompt_fields` — an allowlist of payload paths the LLM
  may see — plus redaction rules for the rest (drop, hash, or mask, e.g.
  usernames hashed so recurrence still works). Applied by `orchestrator`
  before prompt assembly, and by the observability layer before span
  attributes are written.
- Fields marked `secret` never leave `ingestion`.

**Provider terms**

- LLM providers used only under enterprise terms: no training on prompts,
  zero or minimal retention, a data processing agreement in place.
- Provider choice per app can be constrained by data class (see §3 —
  e.g. an app handling `pii` restricted to an approved provider/region).

**Residency**

- Deployments are pinned to a region; Postgres, Kafka, Redis, traces, and
  the LLM endpoint all stay in that region. A multi-region company runs
  one platform deployment per region rather than moving data across.

**Retention**

| Data | Default retention | Notes |
|---|---|---|
| `cases` + `resolution_notes` | Long (e.g. 1–2 years) | The accuracy record and fix history. Each case also stores the full alert payload (ADR-0023), so it carries whatever personal data the payload does |
| `memory_events` | e.g. 90 days, keeping one row per all-time fact (`seen`, confirmed incident) per `alert_key` | Windows only read the last 7 days; older rows only feed `is_novel_alert` / `has_confirmed_incident_history` (ADR-0017). No pruning job in this build |
| `outbox` (`SENT`) | Days | Already pruned (Day 15) |
| Traces (full transcripts) | Short (e.g. 14–30 days) | Most sensitive, least needed long-term |
| Audit log | Per compliance policy | Separate store |

Deletion requests (e.g. a user's data) are honored by deleting or
redacting matching `cases` (including their stored `alert` payload) and traces; `memory-store` holds only
`alert_key`, event kinds and ids, so it has no personal data once usernames
in `alert_key_fields` are hashed.

### Changes to the platform

- Manifest gains `prompt_fields` and redaction rules — additive.
- A redaction step in `orchestrator` and in `ap-shared`'s observability
  module; retention jobs as Celery beat tasks.
- No contract change: `payload` still carries the full validated event
  internally; minimization happens at the prompt and trace boundaries.

**ADR to write**: data classification and prompt minimization.

---

## 3. Model strategy

### Today

One LLM client inside `orchestrator`; model choice is implicit and the
same for every agent. Provider outage behavior isn't specified beyond the
general "fail toward escalation" rule.

### Target

**A model gateway inside `orchestrator`**

- One provider-agnostic interface for every LLM call (messages, tool
  definitions, tool results, token usage). Provider SDKs live behind it.
  The agent loop never calls a provider directly.

**Model per agent, declared in the manifest**

- Each agent gains `model_ref` (e.g. a fast, cheap model for high-volume
  entry agents like `security-triage-agent`; a stronger model for
  `root-cause-summarizer`, which only runs on escalations).
- Versions are pinned (exact model IDs, not "latest"), alongside versioned
  `prompt_ref`s. A model or prompt change is a manifest change — reviewed
  in git (ADR-0006).

**Change control through evals**

- Changing an agent's model or prompt must pass that app's eval set first
  (`plan.md` Day 25), including the adversarial hard gate. The eval
  harness runs per candidate `model_ref`, so model upgrades are compared
  on accuracy, `ESCALATE` recall, latency, and cost — not chosen by
  benchmark reputation.

**Resilience**

- Per-agent fallback chain (e.g. primary model → secondary provider).
  Fallbacks must also have passed the eval gate.
- If every option fails: entry agent → `ESCALATE` with a "not evaluated:
  model unavailable" reason (same path as budget exhaustion, §10);
  callable → "didn't contribute". Never suppression by default.
- Per-provider circuit breakers, as Day 12 does for tools.

**Cost**

- Day 24's spans already record model, tokens, and cost per call; with
  `model_ref` per agent, cost per app and per agent becomes directly
  comparable across models.

### Changes to the platform

- Manifest agents gain `model_ref` and optional `fallback_model_refs` —
  additive.
- Model gateway inside `orchestrator`: since Day 7 the provider seam is
  LangChain's chat-model interface (`BaseChatModel`, built in
  `app/agent/llm.py`, ADR-0022), which replaced the Day 3 interface.
  Per-agent `model_ref` means choosing the chat model per agent when the
  run graph builds it, and fallbacks can use LangChain's model-fallback
  middleware; both are the phase-two additions on top of it.

**ADR to write**: model gateway and per-agent model selection.

---

## 4. Enterprise integrations

### Today

Alerts arrive as each app's own JSON over REST; cases live only in
`review-console`; nothing is sent to or received from other enterprise
systems.

### Target

**Inbound: vendor adapters at `ingestion`**

- Alert sources send their native formats (a monitoring tool's webhook, a
  SIEM's alert export, a cloud cost-anomaly notification). Each app folder
  gains `adapters/` — small, pure mapping functions from a vendor format
  to that app's event schema. `ingestion` picks the adapter by source
  credential. App-owned code, so no platform change per vendor.

**Outbound: case sync to ITSM**

- Many enterprises won't adopt a second case queue; escalations must land
  in ServiceNow/Jira where analysts already work.
- An integration consumer reads `alert.decided` (a new consumer group — no
  producer change, ADR-0002's fan-out) and creates or updates tickets with
  the decision and `reasons[]`. `review-console` remains the system of
  record for verdicts.
- **Bidirectional**: closing a ticket with a resolution in the ITSM maps
  back to a verdict + `resolution_notes` via `review-console`'s API, so the
  feedback loop (Day 10) keeps working when analysts never open
  `review-console`.

**Paging**

- `ESCALATE` decisions above a severity can trigger on-call paging
  (PagerDuty/Opsgenie) through the same outbound integration consumer.
- This is the platform routing a human notification, not an agent action:
  agents still have read-only tools only (ADR-0004). Paging rules live in
  the manifest, not in prompts.

**Real data connectors for tools**

- Replacing fixtures with read-only connectors (metrics/logs queries,
  deploy history, billing APIs, threat intel) per ADR-0009, behind the
  existing tool interfaces. Requires the per-app credential model below.

**Per-app credentials**

- Each app's tool credentials live in a secrets manager under a path
  scoped to its `app_id`; `tool-gateway` fetches them per call using the
  run context's `app_id` and never exposes them to the LLM.
- Before any app holds real credentials, app-owned tools move out of the
  shared `tool-gateway` process (one instance per app or per trust level,
  §12) — closing §13 T9.

### Changes to the platform

- New: integration consumer service (outbound), adapter loading in
  `ingestion`, secrets-manager client in `tool-gateway`.
- Manifest gains `integrations` (ticket target, paging rules) and
  `adapters` — additive.
- `tool-gateway` split per app is a deployment change, same image (§12).

**ADRs to write**: ITSM integration and system of record; per-app
credentials and tool isolation.

---

## 5. Customer (org) tenancy

### Today

A tenant is an app (ADR-0003). Two customers using one app would share
its memory and cases.

### Target

- Add `org_id` alongside `app_id`: on `RunAgentRequest` and the response
  (new proto field numbers — additive), on every app-scoped row and Redis
  key (`mem:{memory_namespace}:{org_id}:{alert_key}:events`), in the
  Kafka message key (`{org_id}:{app_id}:{alert_key}`), budgets, rate
  limits, roles, and audit.
- Apps stay shared definitions; an org may override a small, explicit set
  of manifest fields (guardrails, `prompt_fields`, integrations, budgets),
  never the agent structure.
- Large or regulated customers can get a dedicated deployment of the same
  images (§12) instead of logical isolation.

**Breaks an existing assumption**: the partition key and every storage key
change shape, so this is far cheaper before real data exists than after.
That's the main reason to decide it early if customer tenancy is likely.

**ADR to write**: org tenancy model (logical vs. dedicated deployment).

---

## 6. Operations

| Area | Target |
|---|---|
| **Runtime** | Production Kubernetes, building on the build's local kind setup (ADR-0014: Kustomize, rolling app rollouts, graceful drain): managed multi-node cluster, one Deployment per service, HPA on `orchestrator` bounded by partition count (ADR-0002), managed Kafka/Postgres/Redis |
| **SLOs** | Per app: time from `202` to `alert.decided` (e.g. p95 < 30s), `ESCALATE` recall from evals, zero lost alerts (outbox backlog alarm) |
| **Data durability** | Managed Postgres with backups/PITR; Kafka replication factor ≥ 3; Redis treated as rebuildable cache (already true for memory, §6) |
| **Releases** | Prompt/model/guardrail changes ship via manifest PR + eval gate; service releases via canary on one app's traffic (dedicated deployment, §12) |
| **Incident response** | Kill switch per app (manifest flag → every event `ESCALATE`, "agent disabled") so an app's agent can be pulled instantly without losing alerts |
| **Disaster recovery** | Region-pinned deployments (§2) with documented RTO/RPO; outbox + Kafka retention allow replay after recovery |

---

## 7. Oversight and compliance posture

The design already has the properties governance frameworks for AI
systems tend to ask for; this section maps them, without claiming any
specific certification or legal conformance:

| Expectation | Where the design provides it |
|---|---|
| Human oversight of consequential decisions | Agents only triage; all fixes are human (ADR-0004); escalations reach a person |
| Explainability | `reasons[]` on every decision from the actual tool-call trace (§5) |
| Traceability | Full transcripts in traces (§7); audit log (§1 above) |
| Robustness against manipulation | Threat model (§13), guardrails (ADR-0010), adversarial evals |
| Accuracy monitoring | Verdicts per app; eval harness per model/prompt change |
| Data minimization | `prompt_fields` and redaction (§2 above) |
| Ability to stop the system | Per-app kill switch (§6 above), fail toward escalation |

A real compliance review would still need legal input on the specific
regulations and the domains involved.

---

## 8. Phasing

The order matters: each phase's prerequisites come from the one before.

| Phase | Goal | Includes | Exit criteria |
|---|---|---|---|
| **0 — This build** | Prove the platform | Everything in `plan.md` (27 days) | Three apps, evals, threat model; synthetic data only |
| **1 — Safe for real data** | Close the §13 phase-two gate | Identity and roles, audit log, mTLS + Kafka ACLs, `prompt_fields` + redaction, retention jobs, model gateway | No real data before this exit: T6, T7, T10, T11, T12 closed |
| **2 — First real app** | Real value on one app | Credentials + tool isolation (T9), real read-only connectors for `it-ops-triage`, inbound adapters, ITSM sync, kill switch | Measured noise reduction at an agreed `ESCALATE` recall, on real alerts, with analyst verdicts |
| **3 — Scale out** | More apps and customers | `org_id` tenancy (if needed), per-app dedicated deployments, production Kubernetes, SLOs and DR | Second real app onboarded via manifest + adapters only |

Remediation (ADR-0004) is deliberately absent from every phase: it would
start only after phase 2 produces a verdict-measured accuracy record, as
its own design.
