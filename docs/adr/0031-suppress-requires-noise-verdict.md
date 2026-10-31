# ADR-0031: Opt-in supervisor rule, a SUPPRESS needs a confirmed-noise verdict

- **Status**: Accepted
- **Date**: 2026-10-31
- **See**: `ARCHITECTURE.md` §5, §6, §13 (T3); ADR-0017, ADR-0020, ADR-0010; `plan.md` Day 17

## Context

T3 (memory poisoning, self-reinforcing suppression) says only analyst
verdicts are evidence that an alert is noise. Until now that rule lived
only in prompts. Day 10 already saw it leak in `it-ops-triage`.

Day 17 finished `cost-anomaly-triage`, whose recurring-spike rule is
"SUPPRESS only if at least two past cases on this key carry
`CONFIRMED_NOISE` for the same scheduled job; schedule tags alone never
justify it". Live runs on `reporting-svc:us-east-2` showed the gap. That
key had a spike with `schedule: monthly` tags and no verdicts at all:

- The first alert was novel, so the supervisor's novel-key cap (0.5)
  escalated the agent's SUPPRESS.
- From the second alert on, the key wasn't novel, and the agent
  suppressed it in 1 of 3 runs. In its own words, it quoted the rule and
  then overrode it ("however, the strongest evidence here is the scheduled
  monthly job").

Nothing deterministic could catch this. The `escalate_when` guardrails
can't see past cases. The memory context had `has_confirmed_incident_history`
as an all-time fact, but nothing for noise. Its windowed
`confirmed_noise_count` stops at 7 days, so a monthly job's verdicts never
show in it.

## Decision

- **`memory-store` keeps a third all-time fact, `confirmed_noise`,** next
  to `seen` and `confirmed_incident`. It's derived from the event log like
  the others (any `verdict:CONFIRMED_NOISE` event ever, ADR-0017). It's
  served as `GetContextResponse.has_confirmed_noise_history` (field 8).
  The Redis facts hash gains the field. The cache's `loaded` marker moves
  to version `2`, so an entry cached without the field is a miss and is
  rebuilt from Postgres rather than read as "no noise verdict".
- **The supervisor gets one more cap, opt-in per app.** With the manifest
  setting `supervisor.suppress_requires_noise_verdict: true`, a SUPPRESS
  on a key without `has_confirmed_noise_history` is capped at 0.3, below
  any sensible threshold. Like every supervisor rule, it only moves a
  decision toward ESCALATE (ADR-0020), and the reason names it ("no
  analyst has ever confirmed noise on this alert_key, and this app
  requires that before a SUPPRESS").
- **`registry` validates the setting** as a JSON boolean (default `false`).
  `orchestrator` ignores it when absent, so older stored manifests keep
  their behaviour.
- **`cost-anomaly-triage` turns it on; `it-ops-triage` doesn't.** The
  it-ops agent suppresses flapping noise that nobody has ever reviewed
  (e.g. `healthcheck_flap:lb-02`). Requiring a verdict there would
  escalate all of it until analysts had marked each key once. That's a
  product decision for that app, not something to impose with this change.

Effect for the cost app: a recurring spike with seeded or real
`CONFIRMED_NOISE` verdicts can still be suppressed. A spike whose only
evidence is tags is escalated at least until an analyst confirms it once.
After that it can be suppressed, which is exactly the feedback loop the
platform is for.

## Alternatives considered

- **More prompt wording.** Tried first on Day 17. The stricter rule
  helped (2 of 3 runs escalated on their own) but didn't close the gap,
  and the agent cited the rule while breaking it. Prompt rules stay, as
  guidance; they aren't a control.
- **A manifest guardrail.** `escalate_when` rules see only the alert,
  its payload and the memory context's fields. They can't express
  "SUPPRESS and no noise verdict", because they don't see the decision.
- **Always on, for every app.** Simpler, but it changes `it-ops-triage`'s
  behaviour wholesale (see above). Opt-in keeps that a per-app choice.
- **A longer memory window (e.g. 35 days) instead of an all-time fact.**
  Considered and declined for Day 17's monthly history. An all-time
  boolean answers the question this rule asks ("has an analyst ever said
  noise here?") without picking a window that some cadence (quarterly)
  would still miss.
- **Cap the agent only when the lookup returned no cases.** That's a
  tool result, not memory. It would tie a platform rule to one tool's
  output shape, and a key can have past cases without any noise verdict.

## Consequences

- ✅ "Only verdicts are evidence of noise" is deterministic for apps that
  opt in, whatever the LLM concludes.
- ✅ Additive contract change: a new proto field (older readers ignore
  it), a new optional manifest field, and a cache version bump that
  self-heals by rebuilding.
- ❌ A brand-new recurring job is escalated until an analyst marks it
  once. That's intended, and costs one review per key.
- ❌ An all-time fact never expires. A key marked as noise years ago still
  counts. Acceptable for this build; a retention or decay rule would be
  a later decision (ADR-0017 has no retention for `memory_events` either).
- ❌ `it-ops-triage` keeps the prompt-only version of the rule and its
  known leak (T3), until that app decides to opt in.
