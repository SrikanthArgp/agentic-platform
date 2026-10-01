# ADR-0007: Generic envelope + `Struct` payload; `alert_key` built by `ingestion`

- **Status**: Accepted
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §5, §6; `backend/proto/agent.proto`

## Context

`RunAgentRequest` originally carried only generic fields (`source`,
`severity`, `message`, `timestamp`). Each app's events have their own
shape — cost amounts and tags, security indicators and users — and those
fields had nowhere to go. Separately, "the same alert" (`alert_key`) is
defined differently per app, and nothing said who computes it.

## Decision

- `RunAgentRequest` = a **platform-generic envelope** (named fields) plus
  `google.protobuf.Struct payload`: the full app-specific event exactly as
  `ingestion` validated it against the app's `event_schema_ref`.
- Platform code never interprets `payload`; prompts and app tools do.
- `alert_key` is built by `ingestion` from the manifest's
  `alert_key_fields`, joined with `:` — never by the agent.

## Alternatives considered

- **Per-app proto messages** (`oneof` of app events) — strongly typed, but
  adding an app becomes a proto change and a regeneration for every
  service, breaking "an app is configuration" (ADR-0003).
- **`payload` as a JSON string** — simplest, but opaque to protobuf
  tooling and easier to double-encode.
- **`google.protobuf.Any`** — needs a registered type per app; same
  problem as per-app messages.
- **Agent derives `alert_key`** — non-deterministic; the same event could
  land on different memory keys.

## Consequences

- ✅ New apps need no proto change; one contract serves all apps.
- ✅ The same event always maps to the same memory key and partition.
- ❌ `Struct` numbers are doubles: integer IDs must be sent as strings.
- ❌ `payload` is schema-checked only at `ingestion` (JSON Schema), not by
  protobuf; consumers trust that validation.
