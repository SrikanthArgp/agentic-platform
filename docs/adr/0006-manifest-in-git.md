# ADR-0006: App Manifest source of truth in git, registered by script

- **Status**: Accepted
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §12

## Context

`registry` stores manifests in Postgres and serves them at runtime. But a
manifest references files that live in images (prompts, event schemas,
tool code), and an edit to it changes agent behavior immediately. Edits
needed review and a consistent rollout order.

## Decision

- Source of truth: `backend/apps/{app_id}/manifest.yaml`, reviewed in the
  same commit as the code it references.
- `backend/scripts/register_app.py {app_id}` upserts it into `registry`
  (idempotent). The Postgres row is the runtime copy.
- `registry` validates on registration (tools exist and are read-only,
  exactly one entry agent, `invoke_on`/`escalate_when`/`alert_key_fields`
  well-formed) and rejects with a `4xx` naming the field.
- Rollout order: ship code first, then register the manifest.
- `ingestion` and `orchestrator` cache manifests with the same 30s TTL.

## Alternatives considered

- **Postgres row as source of truth, edited via REST** — no review, no
  history tied to code, easy drift between manifest and images.
- **Manifest baked into images, no registry** — loses "change config
  without redeploy" (Day 5's definition of done).

## Consequences

- ✅ Manifest changes are reviewed, versioned, and traceable to code.
- ✅ Config-only edits still apply within one TTL, no redeploy.
- ❌ `registry` can't validate references into images (prompt files,
  schema fields); those fail at resolve time, per app, explicitly.
- ❌ Someone can still bypass the script via the REST API (no auth in this
  build, §13).
