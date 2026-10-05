# ADR-0021: `escalate_when` rules can read the alert envelope (`alert.*`)

- **Status**: Accepted
- **Date**: 2026-10-05
- **See**: ADR-0010 (amended, not replaced); `ARCHITECTURE.md` §3, §5; `plan.md` Day 7

## Context

ADR-0010 lets a rule's `field` be `payload.<path>` or `context.<path>`, and
sets `it-ops-triage`'s default to `payload.severity in [critical]`. But
severity isn't in the payload: it's in the envelope every app shares
(`RunAgentRequest.severity`, §5), which `ingestion` validates separately
and which `it-ops-triage`'s event schema doesn't repeat. As written, the
it-ops guardrail could never match.

## Decision

- A rule's `field` may also be `alert.<envelope field>`: one of `source`,
  `severity`, `message`, `alert_key`. `registry` rejects any other
  `alert.*` name.
- `it-ops-triage` uses `alert.severity in [critical]`.
  `security-alert-triage`'s defaults become `alert.severity in [high,
  critical]` and `context.has_confirmed_incident_history in [true]`.
- Matching, everywhere: a rule matches when the field's value equals one
  of the `in` values. Types must agree — `true` never equals `1`, and
  `"5"` never equals `5`; numbers compare by value (`3 == 3.0`, since
  `Struct` stores numbers as doubles). Strings are case-sensitive. A
  missing field, an object or list value, or (for `context.*`) no context
  at all, never matches.
- Every rule that matches adds a reason naming it
  (`escalate_when[0] alert.severity = "critical"`), also when the decision
  was already `ESCALATE`; it only changes the decision when it wasn't.

## Alternatives considered

- **Copy severity into every app's payload** — each event schema repeats
  an envelope field, and an app that forgets loses the guardrail silently.
- **Let `payload.severity` fall back to the envelope** — two meanings for
  one path, and a payload that does have its own `severity` would shadow
  the validated one.
- **Case-insensitive or loose (`"1" == 1`) matching** — friendlier, but a
  guardrail is a security bound; it should match exactly what its author
  wrote and be easy to test.

## Consequences

- ✅ Guardrails on the platform's own validated fields work for every app
  without schema changes.
- ✅ Matching rules are visible to the analyst even when the agent already
  escalated.
- ❌ A third prefix to learn; the allowed `alert.*` names are a fixed list
  `registry` and `orchestrator` must agree on.
