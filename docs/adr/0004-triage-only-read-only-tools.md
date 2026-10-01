# ADR-0004: Triage only — no detection, no remediation, read-only tools

- **Status**: Accepted
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §2, §11, §13

## Context

"AI agents for ops" can mean three different jobs: detecting anomalies,
triaging alerts, or fixing problems. Each has very different risk. A
detection engine competes with mature monitoring tools; an agent that
remediates can cause the outage it was meant to fix.

## Decision

- The platform **triages** alerts that external systems already raised.
  It never runs detection logic and never ingests raw telemetry.
- **Every tool is a read-only lookup.** Tool registration requires
  `read_only: true`; `registry` rejects anything else.
- `AUTO_RESOLVE` closes the *alert*; nothing in the affected system is
  touched. Fixes are made by humans after `ESCALATE`.

## Alternatives considered

- **Include detection** — duplicates Prometheus/Datadog/SIEM capability
  and widens scope far beyond the build.
- **Allow remediation tools now** (restart, scale, block IP) — highest
  value per alert, but a wrong or injected decision becomes a production
  action. No accuracy record exists yet to justify that trust.

## Consequences

- ✅ Worst case of a wrong or injected decision is a missed or unneeded
  escalation, never a bad action — this bounds every threat in §13.
- ✅ Analyst verdicts accumulate the accuracy record a future remediation
  step would need.
- ❌ Lower ceiling of value: humans still do all fixing.
- ❌ Real usefulness depends on alert sources outside the platform.

Revisit only with verdict-measured accuracy per app, and then as a new
design: mutating tools marked as such, approval gates, dry runs, rollback.
