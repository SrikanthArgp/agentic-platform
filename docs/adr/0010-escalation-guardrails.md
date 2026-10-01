# ADR-0010: Deterministic `escalate_when` guardrails against prompt injection

- **Status**: Accepted
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §3, §5, §13 (T1, T3)

## Context

Alert payloads go into LLM prompts, and in `security-alert-triage` much of
that text is chosen by an attacker (command lines, usernames, file names).
Crafted text could talk the agent into `SUPPRESS`. Separately, the agent's
own suppression history can make it suppress more (self-reinforcing), and
an attacker can deliberately build "noise" history on a key before a real
attack. Prompt hardening alone cannot guarantee anything against either.

## Decision

- Manifest field `escalate_when: [{field, in}]`. `field` is
  `payload.<path>` or `context.<GetContext field>`. Any match forces
  `ESCALATE`, with a reason naming the rule.
- Evaluated by `orchestrator` after the LLM and the supervisor, before
  callables. Supervisor and guardrails can only move a decision *to*
  `ESCALATE`.
- Defaults: `it-ops-triage` → `payload.severity in [critical]`;
  `security-alert-triage` → `payload.severity in [high, critical]` and
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

## Consequences

- ✅ A hard, testable bound: guarded alerts reach a human no matter what
  the text says.
- ✅ Config, not code — each app tunes its own guardrails.
- ❌ Guardrails only escalate; they can't catch injection that pushes a
  low-severity alert toward suppression. That residual risk is covered by
  evals and analyst sampling, not eliminated.
- ❌ Broad rules raise analyst load; they need tuning against verdicts.
