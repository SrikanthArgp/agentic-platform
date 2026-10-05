# ADR-0018: `memory-store` resolves `memory_namespace` from `registry`

- **Status**: Accepted
- **Date**: 2026-10-05
- **See**: `ARCHITECTURE.md` §3, §6, §12

## Context

The App Manifest's `memory_namespace` prefixes every `memory-store` key
(§3), but `GetContextRequest` and `alert.decided` carry only `app_id`, and
§12 said `memory-store` never reads manifests. Something has to map one to
the other.

## Decision

- `memory-store` looks up the app through `ap-shared`'s `RegistryClient`
  (`GET /apps/{app_id}`, the platform's 30s TTL cache) and uses its
  `memory_namespace` for Redis keys. It reads nothing else from the
  manifest.
- Postgres rows stay keyed by `app_id` (the stable identity); the namespace
  only names cache keys.
- Unknown app: `GetContext` answers `NOT_FOUND`. `registry` unreachable
  with nothing cached: `UNAVAILABLE` (`orchestrator` decides what that
  means for the run). The `alert.decided` consumer retries instead of
  skipping, so no decision is lost.

## Alternatives considered

- **Namespace is always `app_id`** (registry rejects anything else) —
  simplest, but makes a declared manifest field meaningless.
- **Add `memory_namespace` to `GetContextRequest`** — lets a caller choose
  another app's namespace; the server should derive it from `app_id`.

## Consequences

- ✅ `memory_namespace` means what §3 says, and `memory-store` stays the
  only owner of its key layout.
- ❌ `memory-store` gains a runtime dependency on `registry` (cached, and an
  expired copy is served while `registry` is down).
- ❌ Changing an app's `memory_namespace` orphans its old Redis keys until
  they expire; Postgres is unaffected and rebuilds the new keys on demand.
