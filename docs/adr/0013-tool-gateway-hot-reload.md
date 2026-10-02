# ADR-0013: Hot-reload app-scoped tools in `tool-gateway`

- **Status**: Rejected — rolling updates on Kubernetes chosen instead (ADR-0014)
- **Date**: 2026-10-02
- **See**: `ARCHITECTURE.md` §3, §10, §12, §13 (T9, T12); `plan.md` Days 2, 12, 22; ADR-0003, ADR-0006

## Context

`tool-gateway` scans `backend/apps/*/tools/` once, at startup (§12). A new
app or a changed app-owned tool therefore needs a `tool-gateway` restart.
Because `tool-gateway` is shared by every app (ADR-0003), that restart
interrupts all apps: in-flight MCP calls fail, `orchestrator` treats them as
tool failures (§10), and those runs degrade toward `ESCALATE`, so one app's
rollout adds noise to every other app's escalations.

Two limits on what reload alone can fix:

- Reload only helps if new tool code can reach the running container
  without a new image. In this build that's the Compose bind mount of
  `backend/apps` (dev only, §12). In images, the copied `apps/` is still
  authoritative, so production-style rollouts still ship a new image.
- `ingestion` (event schemas) and `orchestrator` (prompts) also read app
  files from their images. Reloading tools alone doesn't make "add an app"
  restart-free. That would take a separate decision.

## Proposal

- **Explicit trigger, never polling**: `POST /admin/reload-tools` on
  `tool-gateway`'s REST port. `register_app.py` calls it *before* upserting
  the manifest, which keeps the ADR-0006 rollout order: tools are loaded
  before any agent is allowed to call them. The script fails and does not
  register if the reload fails. No file watcher: reload happens when someone
  asks for it, not whenever a file changes on disk.
- **Scope: app-scoped tools only.** Global tools (e.g.
  `similar-past-case-lookup`) are platform code in `tool-gateway`'s own
  package and still change only through an image release.
- **Build, validate, then swap atomically**:
  1. Scan `backend/apps/*/tools/` into a *new* tool table. Import each
     module by file path under a fresh, versioned module name (e.g.
     `_apptools.{app_id}.{module}.{content_hash}`), not `importlib.reload`,
     so no stale module state is shared with the old version.
  2. Validate the new table: no duplicate `tool_id`s, every tool declares
     `read_only: true`, and the same per-module checks as startup.
  3. Swap only if the whole scan validates. Replace the table reference in
     one assignment; never mutate the live table.
  4. On any failure, keep the old table, return `4xx`/`5xx` naming the
     failing module, and log it. A broken reload never leaves
     `tool-gateway` with fewer tools than before.
- **In-flight calls finish on the version they started with**: a call
  resolves its tool object once, at dispatch. The swap affects only calls
  that start after it.
- **Removed tools**: a `tool_id` that disappears from disk returns the
  existing explicit not-found error after the swap (§12), never a silent
  omission. Retire tools in this order: manifest first, then code.
- **Observability**: the response and a log line list tools added, changed
  (by content hash) and removed, plus the new table version, and every tool
  span carries the table version. A `tool_table_version` gauge lets Grafana
  show when each instance last reloaded.
- **Multiple replicas** (phase two): each instance reloads separately.
  `register_app.py` must reach every replica, or the rollout uses a rolling
  restart instead. A replica that missed the reload is no worse than today:
  it returns not-found for a tool it doesn't have.

## Alternatives considered

- **Keep restart-only** (current design): simplest, and the image is
  always an exact snapshot of the running code. The cost is that every app
  rollout interrupts every other app.
- **File watcher / periodic rescan**: no extra step, but code goes live
  whenever a file is saved. That breaks the ADR-0006 rollout order and can
  load a half-written module.
- **`importlib.reload` in place**: keeps old module-level state, updates
  modules that import each other inconsistently, and can't be rolled back
  atomically.
- **Tool code fetched remotely (a plugin registry / uploaded bundles)**:
  makes reload useful in production without the bind mount, but turns
  `tool-gateway` into a code-download-and-execute endpoint. Rejected while
  T9/T12 are open. Revisit with signed bundles once `tool-gateway` is split
  per app (§12).
- **Rolling restarts with replicas**: removes downtime without loading new
  code into a running process. This is the right production answer and
  arrives with Kubernetes (`ENTERPRISE_READINESS.md`). It complements
  reload rather than replacing it.

## Consequences if accepted

- ✅ Adding or changing an app's tools no longer interrupts other apps'
  in-flight tool calls; the dev loop with the bind mount needs no
  `tool-gateway` restart.
- ✅ A failed reload is visible and harmless: old tools keep serving.
- ❌ Running code can differ from the image (bind mount), so "the image is
  a reproducible snapshot" (§12) holds only for instances that haven't
  reloaded. The table version in traces and metrics is how that's audited.
- ❌ A new code-loading path in a shared process makes T9 worse: a
  reviewed-but-buggy tool goes live without a release step. The reload
  endpoint is unauthenticated like all internal traffic (T12), but it can
  only load from the `apps/` directory already on disk, never from request
  data.
- ❌ Old module versions stay in memory until restart (Python can't
  reliably unload modules). Fine at the expected reload rate; a periodic
  restart clears it.
- ❌ Does not remove the `ingestion`/`orchestrator` restarts for a new app.

Rejected 2026-10-02 in favour of ADR-0014: rolling updates remove the
shared-restart interruption for all three services, not just
`tool-gateway`, without loading new code into a running process.
