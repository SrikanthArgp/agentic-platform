# ADR-0025: Tool resilience lives in `tool-gateway`, per tool

- **Status**: Accepted
- **Date**: 2026-10-26
- **See**: ADR-0004, ADR-0012; `ARCHITECTURE.md` §10, §12, §13; `plan.md` Day 12

## Context

Before Day 12 a broken tool could hang a run. `tool-gateway` ran a sync
handler on its event loop, so one slow handler stalled every call on that
replica, and `orchestrator`'s MCP call had no timeout. Day 12 adds a
timeout, retry/backoff and a circuit breaker, and checks arguments
against the schema `registry` holds before a tool runs. Day 11 already
covered `tool-gateway` being down: `connect()` fails fast and the run is
"not evaluated".

## Decision

- **The breaker, retries and deadline wrap each tool's handler inside
  `tool-gateway`** (`app/core/resilience.py`), not the MCP call in
  `orchestrator`. There's one breaker per `tool_id`, so one broken tool
  doesn't stop the agent from using the others.
- **One deadline per call** (`TOOL_TIMEOUT_S`, default 5s) covers every
  attempt and backoff. Each attempt gets what's left, so a timed-out
  attempt is never retried and no call takes longer than the deadline.
- **Retries** (`TOOL_MAX_ATTEMPTS`, default 3; backoff 0.1s doubling to a
  1s cap, jittered) are safe because every tool is read-only (ADR-0004).
  Invalid arguments are never retried and never counted by the breaker.
- **The breaker counts failed calls, not attempts**: 5 consecutive calls
  that failed after their retries (timeouts included) open it for 30s
  (`TOOL_BREAKER_FAILURE_THRESHOLD`, `TOOL_BREAKER_COOLDOWN_S`). Then one
  trial call runs while the rest keep failing fast.
- **Two new error codes**, `tool_timeout` and `tool_unavailable`
  (breaker open). `orchestrator` counts both as infrastructure failures,
  like `tool_failed`, so the run escalates and names the tool in
  `reasons[]`.
- **`orchestrator` keeps a plain timeout per MCP call**
  (`TOOL_CALL_TIMEOUT_S`, default 10s, longer than `TOOL_TIMEOUT_S`).
  It fires only if `tool-gateway` itself hangs, and then becomes
  `gateway_unreachable`.
- **Arguments are checked against the input schema `registry` resolved
  for that agent's tool, before the loaded tool's own model.** The
  registered schema is what the agent was offered, so a tool loaded at a
  different version can't accept what `registry` would reject. A missing
  or invalid registered schema refuses the call (`tool_not_allowed`), so
  this fails closed like the allowlist.
- **Sync handlers run in a worker thread**, so a slow one can't block
  the event loop.
- **Breaker state is in memory, per replica.**
- `TOOL_FAULTS` (`app/core/faults.py`, unset in every committed config)
  breaks a named tool on purpose (`error` or `sleep:SECONDS`) for live
  resilience tests.

## Alternatives considered

- **A breaker around the MCP call in `orchestrator`** (what the first
  wording of §10 suggested). It sees only "a call failed": one broken
  tool would trip calls to every tool. Each retry would also be a new
  MCP request. `tool-gateway` knows which tool failed and how.
- **Breaker state shared in Redis.** All replicas would learn at once,
  at the cost of a Redis round trip on every call, and a dependency in
  the tool path that can itself fail. Per replica, each learns within
  `failure_threshold` calls, which is fine for a few replicas.
- **A breaker per (app, tool).** A global tool's handler is shared code,
  so when it breaks it's broken for every app.
- **A deadline per attempt.** Three attempts could take 3x the timeout,
  and `orchestrator` would have to wait that long.
- **`tenacity` / `aiobreaker`.** The code is ~100 lines with an
  injectable clock, which makes timing tests exact. Two libraries would
  be needed to cover the same ground.
- **Process isolation per tool call** (a subprocess or container). Real
  sandboxing, and the only way to stop a hung sync handler; too heavy for
  fixture-backed tools (ADR-0009). It's the phase-two answer once tools
  call real systems.

## Consequences

- ✅ A broken tool costs at most `TOOL_TIMEOUT_S` per call, then nothing
  once its breaker is open, and the run escalates naming it.
- ✅ The registered schema is enforced at the point of execution.
- ❌ A thread can't be cancelled: a hung sync handler keeps its worker
  thread after the timeout. The breaker bounds how many it can take,
  but each replica can still lose up to `failure_threshold` threads per
  hung tool.
- ❌ Each replica's breaker trips on its own, so with N replicas a broken
  tool fails up to N × `failure_threshold` calls before every replica
  stops calling it.
- ❌ Arguments are validated twice (registered schema, then the model).
