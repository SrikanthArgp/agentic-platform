# ADR-0005: One-level, manifest-declared agent delegation (`invoke_on`)

- **Status**: Accepted
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §3, §5

## Context

`it-ops-triage` needs a second agent (`root-cause-summarizer`) that
deepens the explanation on escalations, and other apps may want more
helpers. We needed rules for how many agents an app can have, who decides
when a helper runs, and whether helpers can call helpers.

## Decision

- Exactly one `entry` agent per app; any number of `callable` agents.
- **When a callable runs is declared** in the manifest (`invoke_on:
  [Decision]`), evaluated by `orchestrator` after the entry decision is
  final (post supervisor and guardrails). All matching callables run **in
  parallel**.
- Callables only add `reasons[]`; they never change the decision.
- One level only: no callable → callable, no callable → entry, no
  cross-app calls. Every agent may call tools from its own allowlist.
- A failing, timed-out, or over-budget callable never blocks the decision.

## Alternatives considered

- **LLM-chosen delegation** (callables exposed as tools to the entry
  agent) — flexible, but per-alert cost and latency become unpredictable,
  behavior is hard to test, and injected text could trigger or suppress
  delegation.
- **Arbitrary nesting / agent mesh** — multiplies cost and latency, needs
  cycle detection, and blurs which agent produced which reason.
- **Hardcoding the one summarizer** — breaks the moment an app has two
  callables.

## Consequences

- ✅ Cost and latency per alert are bounded and predictable from the
  manifest; behavior is table-testable.
- ✅ Every reason is attributable to one named agent.
- ❌ Less adaptive: a callable can't be invoked "only when useful" by
  judgment — only by decision value.
- ❌ Deeper analysis must come from more tools, not sub-agents.
