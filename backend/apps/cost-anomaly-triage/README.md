# cost-anomaly-triage

App bundle for app #2, cloud cost-anomaly triage (`docs/ARCHITECTURE.md`
§3, §12; business case in `docs/SCENARIOS.md` §1.2). Built `docs/plan.md`
Day 13, finished Day 17: a recurring spike is suppressed on analysts'
verdicts (past cases), never on tags alone; seeded history; its own
guardrails. Day 17 also added one platform feature this app turns on,
`supervisor.suppress_requires_noise_verdict` (ADR-0031); everything else
here is configuration.

- `manifest.yaml`: the App Manifest, source of truth (ADR-0006). One
  agent, `cost-triage-agent` (entry), two tools (its own `billing-lookup`,
  pinned; the global `similar-past-case-lookup` at `latest`, ADR-0026),
  `event_schema_ref`, `alert_key_fields` (`service:region`, e.g.
  `checkout-svc:us-east-1`), `memory_namespace`, two guardrails
  (`alert.severity in [critical]`, `payload.anomaly_level in [extreme]`)
  and `supervisor` (`min_confidence: 0.6`,
  `suppress_requires_noise_verdict: true`: a SUPPRESS on a key no analyst
  ever marked as noise is escalated, ADR-0031). No `budget`
  or `rate_limit`, so the defaults apply (ADR-0029). `registry` holds the
  runtime copy; after editing, run
  `uv run backend/scripts/register_app.py cost-anomaly-triage`.
- `event_schema.json`: what `ingestion` accepts as this app's `payload`, a
  spend-spike alert: `account`, `service`, `region`,
  `window_start`/`window_end` (ISO 8601 with an offset), `baseline_cost`
  and `observed_cost` (USD) required; `tags` and `anomaly_level`
  (`low|high|extreme`, the detector's grading of the jump) optional; extra
  fields allowed and passed to the agent as data. Tested against the real file
  in `backend/services/ingestion/tests/test_alerts_api.py`.
- `prompts/cost-triage-agent.md`: decides from the billing breakdown
  (untagged spend escalates; tags are claims, not proof), the memory
  context and past cases on the same `service:region`. SUPPRESS needs at
  least two `CONFIRMED_NOISE` past cases for the same scheduled job; a
  `found: false` lookup means ESCALATE. On `ESCALATE`,
  suggests a past case's fix to the analyst when its `resolution_notes`
  name one.

App-owned tool, read-only and fixture-backed (ADR-0004, ADR-0009), loaded
by `tool-gateway` at startup:

- `tools/billing_lookup.py`: `billing-lookup(service, region,
  window_start, window_end)`, total spend with a breakdown per resource
  (with its tags), per tag, and untagged, over whole UTC days (window at
  most 31 days). Backed by `tools/billing.json`, 168 daily line items for
  2026-09-28..10-08, standing in for a cloud billing export:
  - `checkout-svc`/`us-east-1`: a monthly tagged EMR batch on the 1st;
  - `ml-platform`/`eu-west-1`: 8 untagged p4d instances from 10-05;
  - `search-api`/`us-west-2`: tagged autoscaling for a launch on 10-07;
  - `reporting-svc`/`us-east-2`: a tagged `schedule: monthly` EMR export
    on the 1st, like `checkout-svc`'s, but with no past cases: Day 17's
    contrast (tags claim a schedule; no analyst has confirmed it).

Seeded history: `uv run backend/scripts/seed.py --app-id cost-anomaly-triage`
adds three resolved `CONFIRMED_NOISE` cases on `checkout-svc:us-east-1`
(2026-07-01, 08-01, 09-01, the monthly reconciliation batch) plus their
memory events; `--reset` removes them. With it, a spike on that key for
2026-10-01 is suppressed citing those cases.

  Set an alert's window inside those dates to see a breakdown. Tested in
  `backend/services/tool-gateway/tests/test_cost_anomaly_triage_tools.py`.

Platform tool, not in this folder: `similar-past-case-lookup` (global,
`tool-gateway/app/tools/`, ADR-0027) returns this app's resolved cases on
the alert's own key, never another app's. Allowlisted for
`cost-triage-agent` and listed in its `prefetch_tools`, so `orchestrator`
runs it before the agent's loop (ADR-0028).

Try it (stack up, app registered):

```bash
uv run backend/scripts/mcp_call.py --app-id cost-anomaly-triage --agent-id cost-triage-agent \
  billing-lookup '{"service": "ml-platform", "region": "eu-west-1",
  "window_start": "2026-10-05T00:00:00Z", "window_end": "2026-10-08T00:00:00Z"}'
```
