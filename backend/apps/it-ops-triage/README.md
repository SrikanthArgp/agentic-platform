# it-ops-triage

App bundle for the reference IT-ops alert triage app (`docs/ARCHITECTURE.md`
§3, §12). The manifest, event schema, and prompt land in later days of
`docs/plan.md`.

- `tools/lookup_runbook.py`: `lookup_runbook(alert_type)`, read-only, backed
  by the human-curated `tools/runbooks.json` fixture (ADR-0009). Loaded by
  `tool-gateway` at startup; tested in
  `backend/services/tool-gateway/tests/test_it_ops_triage_tools.py`.
