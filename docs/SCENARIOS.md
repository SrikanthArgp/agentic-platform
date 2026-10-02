# Business scenarios

Real-world situations this platform is built for, and where it isn't a
fit. `docs/ARCHITECTURE.md` describes *how* the platform works; this doc
describes *who would pay for it and why*. Use it to frame the README's
problem statement and to tie the measured numbers from `docs/plan.md`
(Days 25–27) to business outcomes.

Every scenario stays inside the platform's scope (`ARCHITECTURE.md` §2,
§11; ADR-0004): an external system has already raised the alert, the
agent **triages** it (`AUTO_RESOLVE` / `ESCALATE` / `SUPPRESS`) using
**read-only** lookups, and a human acts on anything escalated. Numbers
below are illustrative, not measured.

---

## 1. The three apps in this build

### 1.1 SaaS on-call drowning in pages — `it-ops-triage`

- **Situation**: a mid-size SaaS company receives ~3,000
  Prometheus/Datadog alerts a week. On-call engineers ignore most of them,
  and a real outage was missed in the noise last quarter.
- **What the platform does**:
  - suppresses flapping alerts that analysts have confirmed as noise
    (memory, §6)
  - auto-resolves alerts with a known runbook fix (`lookup_runbook`)
  - escalates novel alerts; `root-cause-summarizer` adds a probable cause
    from recent changes, e.g. "deploy `checkout-v2.14` went out 4 minutes
    before the error spike"
- **Business case**: fewer pages, faster detection of what matters, less
  on-call burnout and attrition.
- **Measure**: pages per week, `ESCALATE` recall (no real incident
  suppressed), mean time to acknowledge.

### 1.2 FinOps chasing cloud cost spikes — `cost-anomaly-triage`

- **Situation**: a cloud cost-anomaly detector fires ~40 alerts a month.
  Most are expected (month-end batch jobs, a planned migration), so finance
  has stopped trusting them, and a forgotten GPU cluster burned $60k
  before anyone noticed.
- **What the platform does**:
  - suppresses recurring known spikes using memory of past cycles
  - escalates new spending patterns with a per-service/resource/tag
    breakdown (`billing-lookup`) and similar past cases
- **Business case**: runaway spend is caught within hours, not at
  month-end invoice review.
- **Measure**: dollars caught early, alerts handled per FinOps analyst,
  time until a spike has a named owner.

### 1.3 Understaffed security operations center (SOC) — `security-alert-triage`

- **Situation**: a 4-person security team receives ~1,500 SIEM/EDR alerts
  a day. An internal vulnerability scanner trips the same rules every
  night, and triage is done by gut feel.
- **What the platform does**:
  - checks indicator reputation (`ioc-reputation-lookup`)
  - suppresses analyst-confirmed scanner noise
  - **always** escalates high-severity alerts and known-bad indicators.
    That's a deterministic `escalate_when` guardrail, so attacker-written
    alert text can't talk the agent out of it (§13 T1, ADR-0010).
- **Business case**: analyst time goes to real threats, and every
  decision is explainable for audit (`reasons[]`; supports SOC 2 /
  ISO 27001 evidence).
- **Measure**: alerts handled per analyst, time to triage, missed true
  positives (target zero, enforced by the eval hard gate, `plan.md`
  Day 25).

---

## 2. Further apps on the same core

Each is a folder under `backend/apps/{app_id}/` plus a registered
manifest, with no platform code change and no new services (§3, §12).

| Scenario | Alert source | Tools (read-only) | Why it fits |
|---|---|---|---|
| **Payments fraud review queue** | Fraud-scoring engine flags transactions | Customer history, device reputation, past-case lookup | High volume; decisions must be explainable to regulators; a human makes the final call |
| **Data pipeline failures** | Airflow/dbt run and test failures | Lineage lookup, recent schema changes, run history | Many failures are known flaky upstreams; correlating with recent changes is the key value |
| **CI/CD flaky test triage** | Test failures in CI | Flakiness history, recent commits touching the code | Suppress known flaky tests, escalate real regressions to the right owner |
| **Access review exceptions** | IAM tool flags unusual permission grants | Org chart, ticket lookup, past approvals | Auditors need `reasons[]`; never auto-revoke (read-only principle) |
| **Support-ticket incident signals** | Spike in support tickets on one topic | Status page, recent releases, known-issue lookup | Separates "one unhappy customer" from "an outage everyone is reporting" |
| **Manufacturing / IoT sensor alarms** | SCADA/IoT threshold alarms | Maintenance log, sensor calibration history | Alarm fatigue is a known safety problem; humans must still act |

**Cheapest 4th app to build**: data pipeline failures or CI flaky tests.
Both have easy-to-fixture tools and show a new domain with no platform
change. Add it the same way app #3 ships: `rollout_app.sh {app_id}`
(Day 19, ADR-0014).

---

## 3. Where it doesn't fit

- **Anything that needs automatic action**, such as auto-scaling, blocking
  IPs, or issuing refunds. That's remediation, out of scope by design
  (ADR-0004); `AUTO_RESOLVE` closes the alert, not the problem.
- **Low-volume, high-judgment work** (e.g. legal review): there isn't
  enough repetition for memory and suppression to pay off.
- **Detection itself**: the platform needs another system to raise the
  alert first (§2).
- **Workflows with no human downstream**: escalation is the safety net.
  If nobody reviews `ESCALATE`d cases, the "fail toward escalation" design
  (§13.4) protects nothing.

---

## 4. Measuring business value

Every scenario reduces to the same few numbers, which the build already
produces per `app_id`:

| Business question | Platform metric | Where it comes from |
|---|---|---|
| Are we missing real problems? | `ESCALATE` recall, including the adversarial set | Eval harness (Day 25) |
| How much noise did we remove? | Share of alerts `SUPPRESS`ed / `AUTO_RESOLVE`d that analysts confirmed as correct | Verdicts in `review-console` (Days 9–10) |
| Does it get better with feedback? | Precision/recall before vs. after verdicts on replayed traffic | Simulator replay (Day 21) + eval harness |
| What does it cost? | Cost per 1,000 alerts | LLM observability (Day 24) |
| Is it fast enough? | p95 time from `202` to `alert.decided` | Load test (Day 26) |
| Can we add a use case safely? | Interruptions to existing apps during a new app's rollout | Rollout check (Days 19–20) |
