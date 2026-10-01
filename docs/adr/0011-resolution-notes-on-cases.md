# ADR-0011: Fixes recorded on cases (`resolution_notes`), not in memory

- **Status**: Accepted
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §6, §8, §9

## Context

`memory-store` records whether an `alert_key` repeats and how it was
judged, but nothing recorded *how* an incident was actually fixed. Agents
could therefore never say "last time this was fixed by X".

## Decision

- Verdicts carry an optional free-text `resolution_notes`, stored on the
  `cases` row only (not in the `verdict.recorded` event).
- The global `similar-past-case-lookup` reads cases through
  `review-console`'s `GET /cases`, scoped by the run's `app_id`, and
  returns notes as quoted data. Agents may *suggest* a past fix in
  `reasons[]`; never apply it (ADR-0004).

## Alternatives considered

- **Store fixes in `memory-store`** — mixes free text into a counts/flags
  service and its hot Redis path.
- **Put notes on the Kafka event** — no consumer needs the text.
- **Read the `cases` table directly from `tool-gateway`** — couples a
  service to another's schema and bypasses its `app_id` filtering.

## Consequences

- ✅ Repeat incidents surface the last known fix to the analyst.
- ❌ A new service link (`tool-gateway → review-console`).
- ❌ Analyst-written text enters prompts — an injection path (§13 T2),
  bounded by guardrails and delimiting.
