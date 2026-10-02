# ADR-0009: Fixture-backed tools in this build; real connectors are phase two

- **Status**: Accepted
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §3, §11

## Context

Useful triage and root-cause analysis need real data: metrics, logs,
deploy history, billing, threat intel. Integrating real sources means
credentials, network access, rate limits, and per-app secret isolation —
none of which is designed yet (§13 T9).

## Decision

- Every app-owned tool (`lookup_runbook`, `recent-changes-lookup`,
  `billing-lookup`, `ioc-reputation-lookup`) reads a static JSON fixture.
- Each tool's interface is shaped for its real source, so a connector can
  replace the fixture with no agent or platform change.
- Outputs are framed honestly: `root-cause-summarizer` produces a
  *probable* cause citing evidence, not a diagnosis.

## Alternatives considered

- **One real connector for `it-ops-triage`** (e.g. a Prometheus/Loki query
  plus deploy history) — would prove more real-world value than three
  apps on fixtures. Deferred because it pulls credentials and isolation
  work into the build; it is the recommended first phase-two step.
- **Real connectors for all apps** — out of reach in the build window.

## Consequences

- ✅ Deterministic, offline, testable; the platform mechanics are proven
  without external dependencies.
- ❌ The build demonstrates the platform, not real triage value; accuracy
  numbers are against synthetic data.
- ❌ The credentials model remains an open design question.
