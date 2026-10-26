# ADR-0027: Global tools read their scope from the run context; `alert_key` joins it

- **Status**: Accepted
- **Date**: 2026-10-26
- **See**: ADR-0004, ADR-0011, ADR-0026; `ARCHITECTURE.md` §3, §9, §13 (T2, T4); `plan.md` Day 13

## Context

`similar-past-case-lookup` is the first global tool: platform code
allowlisted by any app. It returns past resolved cases, including
analyst-written `resolution_notes`, so the question is who decides
*which* cases it reads. Since Day 3 the run context `orchestrator` puts
in each MCP request's `_meta` has carried `app_id`, `agent_id` and
`alert_id`, never as tool arguments (§13 T4). The tool also needs the
alert key, which wasn't in the run context. App tools never see the run
context at all: their `TOOLS` contract is a handler of the arguments
alone.

## Decision

- **`RunContext` carries `alert_key`**, set by `orchestrator` from the
  request it's running (`ap-shared`'s `run_context`). Older callers that
  don't send it get an empty string.
- **`similar-past-case-lookup` takes no argument that chooses what it
  reads.** It looks up the run's own `app_id` + `alert_key`; its only
  argument is `limit` (1–10, default 3), and unknown arguments
  (`app_id`, `alert_key`, ...) are `invalid_arguments`. With no
  `alert_key` in the context it returns `found: false` without looking.
- **Global tools live in `tool-gateway`'s own package** (`app/tools/`),
  registered at startup next to the app tools. Only they get the run
  context (`ToolSpec.takes_context`); the app-tool contract is unchanged.
  A global tool's `tool_id` colliding with an app tool's fails startup.
- **It reads `review-console`'s `GET /cases`** (`status=RESOLVED`, the
  run's `app_id` and `alert_key`), never the `cases` table directly. It
  also drops any returned case for another app or key, as a second check.
- **`resolution_notes` is returned as a field of the tool result.** It
  reaches the model inside the data block every tool result is wrapped
  in (§13 T1/T2), and both apps' prompts treat it as reference material:
  something to suggest to the analyst, never to follow or decide on.

## Alternatives considered

- **`alert_key` as a tool argument.** The LLM copies it from the alert,
  but injected text could then make it read other keys in the same app.
  That's a smaller leak than across apps, but a needless one. It's the
  same reasoning as keeping `app_id` out of arguments.
- **Give every tool the run context.** App tools would grow a dependency
  on a platform type, and any app tool could start scoping itself by
  context in ways nobody reviews.
- **Read the `cases` table directly from `tool-gateway`.** That's a
  second owner of `review-console`'s schema, and its query would have to
  re-implement the `app_id` filter instead of using the one `GET /cases`
  already enforces.
- **A similarity search across keys** (embeddings over past alerts).
  Possibly more useful, but it reaches across keys by design, and that
  needs its own isolation story. Same key is the honest first cut.

## Consequences

- ✅ Neither the app nor the alert a lookup reads can be steered by
  prompt text.
- ✅ One implementation serves every app that allowlists it.
- ❌ `tool-gateway` now depends on `review-console` being up for this
  tool. When it's down, the tool fails (`tool_failed`) and the run
  escalates, as for any tool.
- ❌ Only exact-key history: a recurrence under a slightly different key
  (another host, another region) isn't found.
