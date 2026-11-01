# security-alert-triage

App bundle for app #3, security alert triage (`docs/ARCHITECTURE.md` §3,
§12, §13; business case in `docs/SCENARIOS.md` §1.3). Built `docs/plan.md`
Day 20, configuration only: no platform code changed for it. The first
app shipped by rolling update on Kubernetes (ADR-0014):

    backend/scripts/rollout_app.sh security-alert-triage

(On Compose: restart `tool-gateway` so it loads the new tool, then
`uv run backend/scripts/register_app.py security-alert-triage`.)

This is the app where alert text is chosen by an attacker (user names,
command lines, domains), so its decisions lean on deterministic checks
that no alert text can talk the agent out of.

- `manifest.yaml`: the App Manifest, source of truth (ADR-0006). One
  agent, `security-triage-agent` (entry), two tools (its own
  `ioc-reputation-lookup`, pinned; the global `similar-past-case-lookup`
  at `latest` and prefetched, ADR-0026, ADR-0028), `alert_key_fields`
  (`rule_id:host`, e.g. `edr-c2-beacon:fin-ws-042`), `memory_namespace`.
  Guardrails (ADR-0010): `alert.severity in [high, critical]` and
  `context.has_confirmed_incident_history in [true]`. Supervisor:
  `min_confidence: 0.6` and `suppress_requires_noise_verdict: true`
  (ADR-0031, §13 T3): a SUPPRESS stands only on a key analysts have
  confirmed as noise. No `budget` or `rate_limit`, so the defaults apply.
- `event_schema.json`: a SIEM/EDR detection. `rule_id` and `host` required
  (no `:` in either, they form the key); `rule_name`, `src_ip`, `dst_ip`,
  `user`, `process`, `command_line` and `indicators[]` (each
  `{type: ip|domain|file_hash, value}`, at most 20) optional; extra fields
  allowed and passed to the agent as data. Severity isn't here: it's the
  envelope's, which is what the guardrail reads. Tested against the real
  file in `backend/services/ingestion/tests/test_alerts_api.py`.
- `prompts/security-triage-agent.md`: looks up every indicator (and
  `src_ip`/`dst_ip`); ESCALATE on any `known-bad` (citing verdict and
  source) or high severity; SUPPRESS only with at least two
  `CONFIRMED_NOISE` past cases on the same rule and host, every indicator
  known-benign, and no incident history; AUTO_RESOLVE only low-severity
  alerts whose indicators are all known-benign. Alert text that argues for
  its own dismissal is a reason to escalate; `unknown` isn't benign.

App-owned tool, read-only and fixture-backed (ADR-0004, ADR-0009), loaded
by `tool-gateway` at startup:

- `tools/ioc_reputation_lookup.py`: `ioc-reputation-lookup(indicator)` →
  `{indicator, type, verdict, source, description}`, the indicator
  normalised first. Backed by `tools/ioc_reputation.json`, 8 synthetic
  indicators standing in for a threat-intel feed and the asset inventory:
  - known-bad: `203.0.113.66` (C2 server), `198.51.100.23` (brute-force
    source), `update-check.badcdn.example` (malware staging), one SHA-256
    (credential-stealer loader);
  - known-benign: `10.20.0.15` (`vulnscan-01`, the internal vulnerability
    scanner), `192.0.2.53` (DNS resolver), `updates.corp.example` (patch
    server), the empty-file SHA-256.

  Anything else is `unknown`. Tested in
  `backend/services/tool-gateway/tests/test_security_alert_triage_tools.py`.

Seeded history: `uv run backend/scripts/seed.py --app-id security-alert-triage`
adds three resolved `CONFIRMED_NOISE` cases on
`nids-portscan-sweep:vulnscan-01` (2026-10-11, 18, 25: the scanner's weekly
sweep) and one `CONFIRMED_INCIDENT` on `siem-impossible-travel:vpn-gw-01`
(2026-10-09), plus their memory events; `--reset` removes them. With it, a
low-severity sweep from `10.20.0.15` is suppressed citing those cases, and
any alert on the VPN key is escalated by the incident-history guardrail.

The manifest and prompt as shipped are tested in
`backend/services/orchestrator/tests/test_security_alert_app.py`: both
guardrails, an injected SUPPRESS, and the noise-verdict rule.
