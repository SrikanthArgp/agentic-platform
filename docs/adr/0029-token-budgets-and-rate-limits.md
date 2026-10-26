# ADR-0029: Daily token budgets per agent, per-minute rate limits per app and source

- **Status**: Accepted
- **Date**: 2026-10-26
- **See**: ADR-0003, ADR-0005, ADR-0019; `ARCHITECTURE.md` §3, §8, §10, §13 (T5, T6); `plan.md` Day 14

## Context

All apps share one `orchestrator`, and every accepted alert costs LLM
calls with no ceiling. One app's flood (a misconfigured monitor, a broken
integration, spam to the unauthenticated `ingestion`) can run up the bill
and delay every other app's triage. Day 14 adds a spending limit in
`orchestrator` and an intake limit in `ingestion`. Several choices weren't
fixed by the plan.

## Decision

- **Budgets are in tokens, per `app_id` + `agent_id` per UTC day.**
  Counted in a Redis hash, `budget:{app_id}:{agent_id}:{YYYY-MM-DD}`,
  with `input_tokens` and `output_tokens` kept apart. A new UTC day is a
  new key, and old keys expire after 2 days.
- **Limits are set in the manifest**, with platform defaults: an
  optional per-agent `budget: {daily_tokens}` (default
  `DAILY_TOKEN_BUDGET`, 1,000,000) and a per-app `rate_limit: {per_minute,
  per_source_per_minute}` (defaults `RATE_LIMIT_PER_MINUTE` 600,
  `RATE_LIMIT_PER_SOURCE_PER_MINUTE` 300). A change takes effect within
  one manifest TTL, with no restart.
- **A budget is checked before a run and recorded after it.** An entry
  agent that has used its day's budget isn't run at all: no memory call,
  no tools, no model. Its alert is published as `ESCALATE` "not
  evaluated: budget exceeded: …", naming the agent, the usage and the
  limit. A callable over its budget doesn't contribute ("didn't
  contribute (budget exceeded: …)"), and the decision stands.
- **Rate limits use fixed one-minute windows**: `ratelimit:{app_id}:{minute}`
  and `ratelimit:{app_id}:src:{source}:{minute}`, both incremented for
  every event (`INCR` + `EXPIRE` in one transaction). Over either →
  `429` with `Retry-After` set to the seconds left in the window. The
  check runs after the app is resolved (an unknown app is still `404`)
  and before payload validation, so invalid events count too. Refused
  events count as well, so retrying faster earns nothing.
- **Redis unreachable means let the traffic through, and say so.**
  `ingestion` accepts the event and logs a warning. `orchestrator` runs
  the agent, adds the reason "token budget not checked (…); the agent ran
  without that limit." and logs the usage it couldn't record. Both
  clients use short socket timeouts (0.2s and 0.5s), so a Redis outage
  doesn't slow intake or runs down.
- `source` is the envelope's self-declared field; per-source limits trust
  it.

## Alternatives considered

- **Budgets in dollars.** That's what finance wants, but it needs a
  verified price per model, which we don't have for `gpt-5.4-mini`.
  Guessed prices would make a confident-looking wrong number. Day 24
  adds pricing on top of the token counts kept here.
- **Reserve an estimate before the run**, to make the limit exact. A
  run's usage isn't known in advance, so an estimate is either padded
  (refusing runs that would fit) or optimistic (overshooting anyway).
  Check-then-record is simple, and its overshoot is bounded and stated.
- **Sliding-window or token-bucket rate limits.** Smoother at window
  edges (a fixed window allows up to 2× the limit across a boundary),
  but more code and harder to explain in a `Retry-After`.
- **Fail closed when Redis is down.** For budgets, every alert would
  escalate as "not evaluated" for the whole outage, burying analysts in
  exactly the situation where the platform should be helping. For rate
  limits, every alert would be refused at the door, which loses alerts.
  Both limits protect shared capacity; neither is a correctness check.
  `memory-store` being down already works this way: the run goes on and
  says so.
- **Limits in environment variables only.** Every app would get the same
  limit, and lowering one app's would mean a redeploy.

## Consequences

- ✅ Spend per agent per day has a ceiling, and an overrun escalates to a
  human instead of failing silently or queueing.
- ✅ One app's flood can't take another app's intake allowance.
- ✅ The token counters are the raw data for showback and Day 24's cost
  dashboards.
- ❌ A budget isn't exact: the run that crosses it finishes, and N
  concurrent runs that all passed the check can each overshoot by one
  run.
- ❌ A fixed window can let up to twice the per-minute limit through
  across a window boundary.
- ❌ During a Redis outage there are no limits at all. The outage is
  visible in logs and in each decision's reasons, but nothing enforces
  the limits until Redis is back.
- ❌ Per-source limits stop accidents, not a sender that varies `source`;
  per-source credentials are phase two (T6).
