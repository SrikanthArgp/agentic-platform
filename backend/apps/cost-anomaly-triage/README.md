# cost-anomaly-triage

App bundle for app #2, cloud cost-anomaly triage (`docs/ARCHITECTURE.md`
§3, §12; business case in `docs/SCENARIOS.md` §1.2). First cut,
`docs/plan.md` Day 13; Day 17 finishes it (memory-based suppression of a
recurring spike, seeded history, its own guardrails). No platform code
was changed for it.

- `manifest.yaml`: the App Manifest, source of truth (ADR-0006). One
  agent, `cost-triage-agent` (entry), two tools (its own `billing-lookup`,
  pinned; the global `similar-past-case-lookup` at `latest`, ADR-0026),
  `event_schema_ref`, `alert_key_fields` (`service:region`, e.g.
  `checkout-svc:us-east-1`), `memory_namespace`, the `alert.severity in
  [critical]` guardrail and `supervisor.min_confidence: 0.6`. No `budget`
  or `rate_limit`, so the defaults apply (ADR-0029). `registry` holds the
  runtime copy; after editing, run
  `uv run backend/scripts/register_app.py cost-anomaly-triage`.
- `event_schema.json`: what `ingestion` accepts as this app's `payload`, a
  spend-spike alert: `account`, `service`, `region`,
  `window_start`/`window_end` (ISO 8601 with an offset), `baseline_cost`
  and `observed_cost` (USD) required; `tags` optional; extra fields
  allowed and passed to the agent as data. Tested against the real file
  in `backend/services/ingestion/tests/test_alerts_api.py`.
- `prompts/cost-triage-agent.md`: decides from the billing breakdown
  (untagged spend escalates; tags are claims, not proof), the memory
  context and past cases on the same `service:region`. On `ESCALATE`,
  suggests a past case's fix to the analyst when its `resolution_notes`
  name one.

App-owned tool, read-only and fixture-backed (ADR-0004, ADR-0009), loaded
by `tool-gateway` at startup:

- `tools/billing_lookup.py`: `billing-lookup(service, region,
  window_start, window_end)`, total spend with a breakdown per resource
  (with its tags), per tag, and untagged, over whole UTC days (window at
  most 31 days). Backed by `tools/billing.json`, 145 daily line items for
  2026-09-28..10-08, standing in for a cloud billing export:
  - `checkout-svc`/`us-east-1`: a monthly tagged EMR batch on the 1st;
  - `ml-platform`/`eu-west-1`: 8 untagged p4d instances from 10-05;
  - `search-api`/`us-west-2`: tagged autoscaling for a launch on 10-07.

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
