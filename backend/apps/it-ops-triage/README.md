# it-ops-triage

App bundle for the reference IT-ops alert triage app (`docs/ARCHITECTURE.md`
§3, §12).

- `manifest.yaml`: the App Manifest, source of truth (ADR-0006). Two
  agents (`triage-agent`, entry; `root-cause-summarizer`, callable on
  `ESCALATE`), the three tools (two its own, pinned; the global
  `similar-past-case-lookup` at `latest`, ADR-0026), `event_schema_ref`,
  `alert_key_fields` (`alert_type:host`), `memory_namespace`, the
  `alert.severity in [critical]` guardrail and
  `supervisor.min_confidence: 0.6`. No `budget` or `rate_limit`, so
  `orchestrator`'s and `ingestion`'s defaults apply (ADR-0029). `registry`
  holds the runtime copy; after editing, run
  `uv run backend/scripts/register_app.py it-ops-triage`.
- `event_schema.json`: what `ingestion` accepts as this app's `payload`
  (`alert_type` and `host` required; extra fields allowed and passed to
  the agent as data).
- `prompts/`: one per agent. `orchestrator` wraps each in the platform
  rules (data blocks, JSON answer format).
  - `triage-agent.md`: decides from the runbook, the alert data and the
    `memory-store` history, including analyst verdicts (Day 10). On
    `ESCALATE`, suggests a past case's fix to the analyst when its
    `resolution_notes` name one (Day 13), as a suggestion, never an
    action.
  - `root-cause-summarizer.md`: on `ESCALATE`, a probable cause with its
    evidence and a first check; never changes the decision.

App-owned tools, read-only and fixture-backed (ADR-0004, ADR-0009), loaded
by `tool-gateway` at startup:

- `tools/lookup_runbook.py`: `lookup_runbook(alert_type)`, backed by the
  human-curated `tools/runbooks.json`. Allowlisted for both agents. Tested
  in `backend/services/tool-gateway/tests/test_it_ops_triage_tools.py`.
- `tools/recent_changes_lookup.py`: `recent-changes-lookup`, deploys and
  config/infra changes to a service or host in a time window, backed by
  `tools/recent_changes.json` (absolute timestamps around 2026-10-05).
  Allowlisted for `root-cause-summarizer` only. Tested in
  `backend/services/tool-gateway/tests/test_recent_changes_lookup.py`.

Platform tool, not in this folder: `similar-past-case-lookup` (global,
`tool-gateway/app/tools/`, ADR-0027) returns this app's resolved cases on
the alert's own key. Allowlisted for `triage-agent` and listed in its
`prefetch_tools`, so `orchestrator` runs it before the agent's loop and
puts the result in its first message (ADR-0028).
