# ADR-0028: Manifest-declared `prefetch_tools`, run by `orchestrator` before the loop

- **Status**: Accepted
- **Date**: 2026-10-26
- **See**: ADR-0019, ADR-0022, ADR-0025, ADR-0027; `ARCHITECTURE.md` §3, §5; `plan.md` Day 13

## Context

Day 13 gave `triage-agent` and `cost-triage-agent` the global
`similar-past-case-lookup`, and told both prompts to call it when the
memory context shows an analyst verdict. On the live stack, with
`has_confirmed_incident_history: true` and a resolved case on the key,
`triage-agent` called it on 0 of 3 runs. `cost-triage-agent` called it
on 1 of 2, and only after the instruction was moved up the prompt. A
lookup whose whole value is to be there every time can't depend on the
LLM remembering to make it. ADR-0019 settled the same question for
memory context: fetch it before the loop, don't offer it as a tool.

## Decision

- **An agent may list `prefetch_tools` in the manifest.** Before that
  agent's loop, `orchestrator` calls each of them with no arguments,
  through the run's MCP session and run context, and puts each result in
  the agent's first message under "Looked up before you started", as a
  `tool_result:{tool_id}` data block like any other tool result. Entry
  and callable agents both support it.
- **A prefetch is an ordinary tool call** in every other respect: it's
  in the trace (`tool_calls[]` with the agent's `agent_id`), goes through
  `tool-gateway`'s allowlist, schema, version and resilience checks, and
  an infrastructure failure escalates the run like any failed lookup.
- **`registry` validates it**: each prefetched tool must be on that
  agent's `tool_allowlist` and accept `{}` against its registered input
  schema.
- **A prefetched tool stays offered** to the agent, and the platform
  prompt says not to call it again. A prefetched tool that isn't offered
  right now (disabled) is skipped without a call.
- **Which tool is named only in the manifest.** `orchestrator` has no
  tool-specific code; both apps list `similar-past-case-lookup`.

## Alternatives considered

- **Keep it prompt-driven and measure the rate** (Day 25 evals). Cheaper,
  but the suggestion would stay hit-or-miss on exactly the repeat
  incidents it's for.
- **Hard-code the lookup in `orchestrator`** when the context shows a
  verdict. That puts one global tool's name and its trigger into
  platform code. The manifest field covers this case and any later one.
- **Prefetch only when a condition on the memory context holds** (e.g. a
  verdict exists). The tool returns `found: false` cheaply when there's
  nothing, and resolved cases only exist after a verdict, so the
  condition would add a rule language for no change in outcome.
- **Have the platform write the suggestion into `reasons[]`
  deterministically.** That would put analyst free text into reasons
  without the agent weighing it against the current alert, and teach
  `orchestrator` one tool's output shape. `review-console` showing past
  cases on the key directly is the better deterministic route, if one is
  needed.

## Consequences

- ✅ The past-case lookup runs on every run of an agent that declares it
  (7 of 7 on the live stack, across both apps).
- ✅ One fewer model round when the agent would have called it anyway.
- ❌ Whether the agent *passes the past fix on* in its reasons is still
  the LLM's call: 4 of those 7 runs did. That's for Day 25's evals to
  measure.
- ❌ Every run of these agents now makes a `review-console` call, even
  for a key with no history, and `review-console` being down escalates
  every run of them, not just the ones that would have looked.
