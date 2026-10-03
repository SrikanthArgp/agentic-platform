# it-ops-triage

App bundle for the reference IT-ops alert triage app (`docs/ARCHITECTURE.md`
§3, §12). The event schema lands Day 5 of `docs/plan.md`.

- `manifest.yaml`: partial App Manifest (agents, tools). Read from disk by
  `orchestrator` until Day 5, when `registry` serves it.
- `prompts/triage-agent.md`: the entry agent's app-specific instructions.
  `orchestrator` wraps them in the platform rules (data blocks, JSON answer
  format).

- `tools/lookup_runbook.py`: `lookup_runbook(alert_type)`, read-only, backed
  by the human-curated `tools/runbooks.json` fixture (ADR-0009). Loaded by
  `tool-gateway` at startup; tested in
  `backend/services/tool-gateway/tests/test_it_ops_triage_tools.py`.
