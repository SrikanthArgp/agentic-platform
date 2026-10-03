# it-ops-triage

App bundle for the reference IT-ops alert triage app (`docs/ARCHITECTURE.md`
§3, §12).

- `manifest.yaml`: partial App Manifest (agents, tools, `event_schema_ref`,
  `alert_key_fields`). Read from disk by `orchestrator` and `ingestion`
  until Day 5, when `registry` serves it.
- `event_schema.json`: what `ingestion` accepts as this app's `payload`
  (`alert_type` and `host` required; extra fields allowed and passed to
  the agent as data).
- `prompts/triage-agent.md`: the entry agent's app-specific instructions.
  `orchestrator` wraps them in the platform rules (data blocks, JSON answer
  format).

- `tools/lookup_runbook.py`: `lookup_runbook(alert_type)`, read-only, backed
  by the human-curated `tools/runbooks.json` fixture (ADR-0009). Loaded by
  `tool-gateway` at startup; tested in
  `backend/services/tool-gateway/tests/test_it_ops_triage_tools.py`.
