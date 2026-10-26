# ADR-0026: Tool versions are pinned or `latest`; `tool-gateway` refuses a version it didn't load

- **Status**: Accepted
- **Date**: 2026-10-26
- **See**: ADR-0006, ADR-0014, ADR-0025, ADR-0027; `ARCHITECTURE.md` §3, §12; `plan.md` Day 13

## Context

Since Day 5 a manifest has declared each tool at an exact version, and
`registry` keys tools by `(tool_id, version)`. Day 13 asks for "latest or
a pinned version". Two facts shape what that can mean:

- `tool-gateway` loads exactly **one** version of each tool: whatever
  code its image has. Nothing checked that it matched the version
  `registry` resolved, so a manifest pinned at `1.0.0` would silently run
  `1.1.0` code if the image had moved on.
- Agents have a `version` field, but agents aren't registered on their
  own. They exist only inside their app's manifest, with their prompt as a
  file in `orchestrator`'s image. There's nothing to resolve.

## Decision

- **A manifest tool reference is `MAJOR.MINOR.PATCH` or `latest`.**
  `latest` is the highest registered version by number (`1.10.0` >
  `1.9.0`). It's resolved on every `GET /apps/{app_id}`, so registering a
  new version moves every `latest` manifest onto it within one
  manifest-cache TTL, with no re-registration. The served manifest gives
  both, per tool: `version` (resolved) and `requested_version`.
- **`latest` ignores `enabled`.** Disabling the newest version turns the
  tool off; it never quietly falls back to an older one.
- **`tool-gateway` refuses a call whose resolved version isn't the one it
  loaded**, with `tool_version_mismatch`, before running anything.
  `orchestrator` counts it as an infrastructure failure, so the run
  escalates naming the tool. This fails closed, like the allowlist.
- **Deleting a version**: refused (`409`) if any app pins it, or if an app
  is on `latest` and it's the only registered version. Deleting the
  newest of several is allowed and moves `latest` back one, which is how
  a global tool's upgrade is rolled back, together with rolling back
  `tool-gateway`.
- **`register_app.py`** registers a `latest` tool at whatever version
  `tool-gateway` serves, and still fails if it serves none.
- **Convention**: app-owned tools are pinned (they ship in the same
  folder and commit as the manifest); global tools are `latest` (the
  platform upgrades them for every app at once).
- **Agents are not versioned beyond the manifest field.** An agent's
  version changes when its app's manifest does; `registry` stores it and
  `GET /agents` lists it, but resolves nothing by it.

## Alternatives considered

- **`tool-gateway` serves several versions of a tool at once**, keyed by
  `(tool_id, version)`. It's the only way two apps could pin different
  versions of one global tool at the same time. It's a much bigger
  change: loader layout, module naming and MCP tool names would all need
  to carry the version. Not needed while every tool ships with the
  platform image.
- **Semver ranges** (`^1.2`, `~1.2.3`). They'd add a resolution rule to
  learn, for no case we have.
- **`latest` = newest *enabled* version.** Disabling a bad release would
  silently move apps onto older code, and the kill switch would stop
  meaning "off".
- **Trust the version and skip the check in `tool-gateway`.** The
  mismatch would be invisible: the agent would be offered the registered
  schema and description while different code ran.
- **Resolve `latest` once, at registration.** A new global tool version
  would then need every app re-registered.

## Consequences

- ✅ A version skew between `registry` and `tool-gateway` is a named,
  escalated failure instead of silent wrong code.
- ✅ A global tool upgrade reaches every `latest` app with one
  registration, and rolls back with one delete.
- ❌ During a rolling upgrade of `tool-gateway`, calls can hit
  `tool_version_mismatch` until the image and `registry` agree (≤ one TTL
  after both are updated), and those runs escalate.
- ❌ Two apps can't pin different versions of the same tool at the same
  time.
