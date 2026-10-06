# ADR-0010: Deterministic `escalate_when` guardrails against prompt injection

- **Status**: Accepted
- **Date**: 2026-10-01 (envelope fields and matching rules added 2026-10-05, Day 7)
- **See**: `ARCHITECTURE.md` §3, §5, §13 (T1, T3)

## Context

Alert payloads go into LLM prompts, and in `security-alert-triage` much of
that text is chosen by an attacker (command lines, usernames, file names).
Crafted text could talk the agent into `SUPPRESS`. Separately, the agent's
own suppression history can make it suppress more (self-reinforcing), and
an attacker can deliberately build "noise" history on a key before a real
attack. Prompt hardening alone cannot guarantee anything against either.

## Decision

- Manifest field `escalate_when: [{field, in}]`. `field` is one of:
  - `alert.<envelope field>`: `source`, `severity`, `message` or
    `alert_key` — the `RunAgentRequest` fields every app shares, which
    `ingestion` validates. `registry` rejects any other `alert.*` name.
  - `payload.<path>`: a value in the app's event payload.
  - `context.<path>`: a scalar field of the run's `GetContextResponse`.
- **Matching**: a rule matches when the field's value equals one of the
  `in` values. Types must agree — `true` never equals `1`, `"5"` never
  equals `5`; numbers compare by value (`3 == 3.0`, since `Struct` stores
  numbers as doubles). Strings are case-sensitive. A missing field, an
  object or list value, or (for `context.*`) no context at all, never
  matches.
- Any match forces `ESCALATE`. Every matching rule adds a reason naming it
  (`escalate_when[0] alert.severity = "critical"`), also when the decision
  was already `ESCALATE`.
- Evaluated by `orchestrator` after the LLM and the supervisor, before
  callables. Supervisor and guardrails can only move a decision *to*
  `ESCALATE`.
- Defaults: `it-ops-triage` → `alert.severity in [critical]`;
  `security-alert-triage` → `alert.severity in [high, critical]` and
  `context.has_confirmed_incident_history in [true]`.
- Complementary, not replaced: payload/tool results passed as delimited
  data; only analyst verdicts count as evidence of noise; adversarial
  eval cases are a hard gate.

## Alternatives considered

- **Prompt hardening only** — necessary but unbounded: no prompt reliably
  resists injection.
- **A second LLM as an injection classifier** — doubles cost, and is
  itself injectable.
- **A general rules engine / policy language** — more expressive, but a
  large surface for a need that's currently "these fields with these
  values must escalate".
- **Strip or escape payload text** — breaks triage; the suspicious text is
  often the signal.
- **Severity in the payload (`payload.severity`)** — the first draft of
  this ADR. Severity is an envelope field, not repeated in app schemas, so
  that rule could never match; copying it into every schema means an app
  that forgets loses the guardrail silently, and falling back from
  `payload.severity` to the envelope gives one path two meanings.
- **Case-insensitive or loose (`"1" == 1`) matching** — friendlier, but a
  guardrail is a security bound; it should match exactly what its author
  wrote and be easy to test.

## Consequences

- ✅ A hard, testable bound: guarded alerts reach a human no matter what
  the text says.
- ✅ Config, not code — each app tunes its own guardrails, including on the
  platform's own validated envelope fields, with no schema changes.
- ✅ Matching rules are visible to the analyst even when the agent already
  escalated.
- ❌ Guardrails only escalate; they can't catch injection that pushes a
  low-severity alert toward suppression. That residual risk is covered by
  evals and analyst sampling, not eliminated.
- ❌ Broad rules raise analyst load; they need tuning against verdicts.
- ❌ The allowed `alert.*` names are a fixed list that `registry` and
  `orchestrator` must agree on.
