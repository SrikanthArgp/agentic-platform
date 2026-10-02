# ADR-0003: Apps are configuration on a shared runtime; a tenant is an app

- **Status**: Accepted
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §1, §3, §12

## Context

The platform must run several unrelated triage use cases (IT-ops, cloud
cost, security) and prove a new one can be added without touching platform
code. We had to decide what an "app" physically is, what "tenant" means,
and how an alert finds its app.

## Decision

- **An app = a registered App Manifest + a folder** `backend/apps/{app_id}/`
  (manifest, event schema, prompts, app-owned tools, simulator scenarios).
  It is not a service or container.
- **One container per platform service, never per app.** One
  `orchestrator` runs every app's agents; an agent is a manifest entry and
  each alert gets an ephemeral run of it.
- **Routing is by URL** (`POST /apps/{app_id}/events`), chosen by the alert
  source — never inferred by an LLM or classifier.
- **A tenant is an app.** Isolation is by `app_id` on every message and
  row, `memory_namespace` on every memory key, and per-agent tool
  allowlists. There is no per-customer (`org_id`) tenancy.

## Alternatives considered

- **A service or container per app** — strong runtime isolation, but
  duplicates the platform per app and makes "add an app" a deployment
  project, defeating the point.
- **LLM-based routing** ("which app is this alert?") — costs tokens, is
  non-deterministic, and can be steered by crafted alert text into another
  app's memory and tools.
- **Per-customer tenancy now** — no customer exists in this build; adding
  `org_id` everywhere is cheap later only if done before real data exists
  (noted in §11).

## Consequences

- ✅ A new app is a folder plus `register_app.py`; Days 17–22 prove it with
  zero platform code changes.
- ✅ Deterministic routing; isolation is testable (colliding-key tests).
- ❌ No runtime isolation between apps: one app's flood shares
  `orchestrator` capacity (mitigated by per-app rate limits and budgets),
  and all app tool code shares one `tool-gateway` process (§13 T9).
- ❌ Two customers on one app would share memory and cases.

Revisit when a customer-facing deployment exists (add `org_id`) or an app
needs hard isolation (dedicated deployment of the same image, §12).
