# registry — class diagram

As built through Day 14 of `docs/plan.md` (Day 13: version references
pinned or `latest`, JSON-Schema checks on tools, `prefetch_tools`; Day 14:
`budget` per agent and `rate_limit` per app). `registry` stores two kinds of
records: **tool registrations** (one per `tool_id` + `version`) and **App
Manifests** (one per `app_id`). It validates a manifest when it is
registered, and serves it back *resolved*: each manifest tool reference
resolved to a registered version, and each agent carrying the tools it
may call right now. `orchestrator`, `ingestion` and `tool-gateway` read
that resolved app through `ap-shared`'s `registry_client` (30s TTL cache);
`memory-store` reads its `memory_namespace` the same way;
`backend/scripts/register_app.py` writes it.

Python modules that are plain functions (no class) are drawn as
`<<module>>` boxes. Third-party and cross-service types are in the
`external` group.

## Overview: layers

```mermaid
classDiagram
    direction TB
    class API {
        <<layer: FastAPI routers>>
        api/tools.py
        api/apps.py
    }
    class Core {
        <<layer: pure logic>>
        models (pydantic)
        validation.validate_manifest
        versions.resolve_ref
        resolve.resolve_app
    }
    class Port {
        <<layer: Protocol>>
        Repository
    }
    class Adapters {
        <<layer>>
        PostgresRepository (asyncpg)
        InMemoryRepository (tests)
    }
    API --> Core : validate, resolve
    API --> Port : read / upsert
    Adapters ..|> Port : implement
```

The routers hold no business rules beyond simple field checks: cross-field
manifest rules live in `validation.py`, "which registered version does this
reference mean" in `versions.py`, the "which tools may this agent
call" rule in `resolve.py`, and storage sits behind `Repository`, so
every rule is unit-tested against the in-memory repository with no
Postgres.

## Full class diagram

```mermaid
classDiagram
    direction LR

    namespace entrypoint {
        class main {
            <<module: app.main>>
            +SERVICE_NAME = "registry"
            +DEFAULT_POSTGRES_DSN
            +create_app(repo=None) FastAPI
            +app: FastAPI
            +healthz() HealthResponse
        }
        class HealthResponse {
            <<pydantic>>
            +status: str
            +service: str
        }
    }

    namespace api {
        class tools_router {
            <<module: app.api.tools>>
            PUT /tools/~tool_id~/versions/~version~
            GET /tools
            GET /tools/~tool_id~/versions/~version~
            PATCH /tools/~tool_id~/versions/~version~
            DELETE /tools/~tool_id~/versions/~version~
        }
        class apps_router {
            <<module: app.api.apps>>
            PUT /apps/~app_id~
            GET /apps
            GET /apps/~app_id~
            DELETE /apps/~app_id~
            GET /agents
            -_declared_tools(repo, manifest) dict
        }
        class AppSummary {
            <<pydantic>>
            +app_id: str
            +display_name: str
            +updated_at: datetime?
        }
        class AgentSummary {
            <<pydantic>>
            +app_id: str
            +agent_id: str
            +version: str
            +role: str
        }
    }

    namespace core_input {
        class ToolIn {
            <<pydantic, extra=forbid>>
            +description: str
            +scope: app or global
            +app_id: str?
            +input_schema: dict
            +output_schema: dict?
            +read_only: bool
        }
        class ToolEnabledIn {
            <<pydantic, extra=forbid>>
            +enabled: bool
        }
        class ManifestIn {
            <<pydantic, extra=forbid>>
            +app_id: str
            +display_name: str
            +agents: list~AgentIn~
            +tools: list~ToolRefIn~
            +event_schema_ref: str
            +alert_key_fields: list~str~
            +memory_namespace: str
            +escalate_when: list~EscalateRuleIn~
            +supervisor: SupervisorIn?
            +rate_limit: RateLimitIn?
            +stored() dict
        }
        class SupervisorIn {
            <<pydantic, extra=forbid>>
            +min_confidence: float? 0-1
        }
        class RateLimitIn {
            <<pydantic, extra=forbid>>
            +per_minute: int? ≥1
            +per_source_per_minute: int? ≥1
        }
        class AgentIn {
            <<pydantic, extra=forbid>>
            +agent_id: str
            +version: str
            +role: entry or callable
            +prompt_ref: str
            +tool_allowlist: list~str~
            +invoke_on: list~str~
            +prefetch_tools: list~str~
            +budget: BudgetIn?
        }
        class BudgetIn {
            <<pydantic, extra=forbid>>
            +daily_tokens: int? ≥0
        }
        class ToolRefIn {
            <<pydantic, extra=forbid>>
            +tool_id: str
            +version: x.y.z or latest
            +scope: app or global
        }
        class EscalateRuleIn {
            <<pydantic, extra=forbid>>
            +field: str
            +in_: list~Scalar~ (alias "in")
        }
    }

    namespace core_stored {
        class Tool {
            <<pydantic>>
            +tool_id, version: str
            +description: str
            +scope: app or global
            +app_id: str?
            +input_schema: dict
            +output_schema: dict?
            +read_only: bool
            +enabled: bool = True
            +created_at, updated_at
            +definition() dict
        }
        class AppRecord {
            <<pydantic>>
            +app_id: str
            +display_name: str
            +manifest: dict
            +created_at, updated_at
        }
        class UpsertResult {
            <<pydantic>>
            +created: bool
            +changed: bool
        }
    }

    namespace core_served {
        class ResolvedApp {
            <<pydantic: GET /apps/~app_id~>>
            +app_id, display_name: str
            +agents: list~ResolvedAgent~
            +tools: list~ResolvedToolRef~
            +event_schema_ref: str
            +alert_key_fields: list~str~
            +memory_namespace: str
            +escalate_when: list~dict~
            +supervisor: dict?
            +rate_limit: dict?
            +updated_at: datetime?
        }
        class ResolvedAgent {
            <<pydantic>>
            +agent_id, version, role, prompt_ref
            +tool_allowlist: list~str~
            +invoke_on: list~str~
            +prefetch_tools: list~str~
            +budget: dict?
            +tools: list~EffectiveTool~
        }
        class EffectiveTool {
            <<pydantic>>
            +tool_id, version: str
            +scope: app or global
            +description: str
            +input_schema: dict
        }
        class ResolvedToolRef {
            <<pydantic>>
            +tool_id: str
            +version: str?
            +requested_version: str
            +scope: app or global
            +enabled: bool
        }
    }

    namespace core_logic {
        class validation {
            <<module: app.core.validation>>
            +DECISIONS: frozenset
            +ENVELOPE_FIELDS: tuple
            +validate_manifest(manifest, url_app_id, lookup_tool) list~FieldError~
            -_check_tools(manifest, lookup_tool)
            -_check_agents(manifest)
            -_check_prefetch(manifest, lookup_tool)
            -_escalate_field_problem(field)
            -_context_path_problem(path)
        }
        class FieldError {
            <<dataclass, frozen>>
            +field: str
            +message: str
        }
        class resolve {
            <<module: app.core.resolve>>
            +resolve_app(record, tools) ResolvedApp
        }
        class versions {
            <<module: app.core.versions>>
            +LATEST = "latest"
            +version_key(version) tuple~int~
            +newest(tools) Tool?
            +resolve_ref(requested, versions) Tool?
        }
    }

    namespace db {
        class Repository {
            <<Protocol>>
            +get_tool(tool_id, version) Tool?
            +list_tools() list~Tool~
            +list_tool_versions(tool_id) list~Tool~
            +upsert_tool(tool) UpsertResult
            +set_tool_enabled(tool_id, version, enabled) Tool?
            +delete_tool(tool_id, version) bool
            +get_app(app_id) AppRecord?
            +list_apps() list~AppRecord~
            +upsert_app(app_id, display_name, manifest) UpsertResult
            +delete_app(app_id) bool
        }
        class PostgresRepository {
            -_pool: asyncpg.Pool
        }
        class InMemoryRepository {
            -_tools: dict
            -_apps: dict
        }
        class postgres {
            <<module: app.db.postgres>>
            +create_pool(dsn) asyncpg.Pool
            -_init_connection(conn) json/jsonb codecs
        }
    }

    namespace external {
        class FastAPI { <<fastapi>> }
        class asyncpg_Pool { <<asyncpg>> }
        class GetContextResponse { <<proto: memory_store_pb2>> }
        class Decision { <<proto enum: agent_pb2>> }
        class Draft202012Validator { <<jsonschema>> }
        class observability { <<ap-shared>> }
    }

    main ..> FastAPI : creates
    main ..> HealthResponse : /healthz
    main ..> observability : at import
    main --> tools_router : include_router
    main --> apps_router : include_router
    main ..> PostgresRepository : lifespan, when repo is None
    main ..> postgres : create_pool

    tools_router ..> ToolIn : body (PUT)
    tools_router ..> ToolEnabledIn : body (PATCH)
    tools_router ..> Tool : builds, returns
    tools_router ..> UpsertResult : returns
    tools_router ..> Draft202012Validator : check_schema (PUT)
    tools_router ..> versions : LATEST (DELETE)
    tools_router --> Repository : app.state.repo

    apps_router ..> ManifestIn : body (PUT)
    apps_router ..> versions : resolve_ref per declared tool
    apps_router ..> validation : validate_manifest
    apps_router ..> resolve : resolve_app (GET)
    apps_router ..> AppSummary
    apps_router ..> AgentSummary
    apps_router --> Repository : app.state.repo

    ManifestIn *-- AgentIn
    ManifestIn *-- ToolRefIn
    ManifestIn *-- EscalateRuleIn
    ManifestIn *-- SupervisorIn
    ManifestIn *-- RateLimitIn
    AgentIn *-- BudgetIn

    ResolvedApp *-- ResolvedAgent
    ResolvedApp *-- ResolvedToolRef
    ResolvedAgent *-- EffectiveTool

    validation ..> ManifestIn : checks
    validation ..> Tool : via lookup_tool
    validation ..> FieldError : returns
    validation ..> GetContextResponse : context.* paths
    validation ..> Decision : invoke_on values
    validation ..> Draft202012Validator : prefetch callable with {}
    versions ..> Tool : picks one
    resolve ..> AppRecord : reads manifest
    resolve ..> Tool : enabled?
    resolve ..> ResolvedApp : builds

    PostgresRepository ..|> Repository
    InMemoryRepository ..|> Repository
    PostgresRepository --> asyncpg_Pool
    Repository ..> Tool
    Repository ..> AppRecord
    Repository ..> UpsertResult
```

## Classes and modules

### Entry point — `app/main.py`

- **`create_app(repo=None)`**: builds the FastAPI app, mounts both
  routers, and stores the repository on `app.state.repo`. Tests pass an
  `InMemoryRepository`; otherwise the lifespan opens an asyncpg pool on
  `POSTGRES_DSN` (default `postgresql://platform:platform@localhost:5432/platform`)
  and wraps it in a `PostgresRepository`, closing it on shutdown.
- **`app = create_app()`**: the module-level app uvicorn runs.
- **`GET /healthz`**: `HealthResponse {status, service}`, polled by
  Compose. Postgres being reachable is checked implicitly: the pool is
  opened at startup, so a dead database fails the container, not the
  health check.

### API — `app/api/tools.py`

| Route | Body → response | Notes |
|---|---|---|
| `PUT /tools/{tool_id}/versions/{version}` | `ToolIn` → `UpsertResult` (`201` created, `200` otherwise) | `read_only` must be true; `app_id` required for `scope: app`, absent for `global`; `input_schema` and `output_schema` must be valid JSON Schema (`422` naming the field, Day 13: `tool-gateway` validates every call against it). Never touches `enabled`. |
| `GET /tools` | → `list[Tool]` | Sorted by `tool_id`, `version`. |
| `GET /tools/{tool_id}/versions/{version}` | → `Tool` | `404` if unknown. |
| `PATCH /tools/{tool_id}/versions/{version}` | `ToolEnabledIn` → `Tool` | The runtime switch. Takes effect in readers on their next fetch (≤30s). |
| `DELETE /tools/{tool_id}/versions/{version}` | → `204` | `409` while an app pins that version, or declares `latest` and it's the only version left (disable instead). Deleting the newest of several moves `latest` back one: how a global tool's upgrade is rolled back (ADR-0026). |

Path parameters are pattern-checked (`TOOL_ID`, `VERSION` = `x.y.z`), so a
malformed id is a `422` before any lookup.

### API — `app/api/apps.py`

| Route | Body → response | Notes |
|---|---|---|
| `PUT /apps/{app_id}` | `ManifestIn` → `UpsertResult` (`201`/`200`) | Shape errors: pydantic `422`. Cross-field errors: `422 {message, errors: [{field, message}]}` from `validate_manifest`, all at once. Unchanged manifest → `changed: false`. |
| `GET /apps` | → `list[AppSummary]` | |
| `GET /apps/{app_id}` | → `ResolvedApp` | What the three readers cache. `404` if unknown. |
| `DELETE /apps/{app_id}` | → `204` | |
| `GET /agents` | → `list[AgentSummary]` | Agents across all apps, read from manifests. |

`_declared_tools(repo, manifest)` maps each declared `tool_id` to the
registered `Tool` its version reference resolves to
(`versions.resolve_ref` over `list_tool_versions`). `PUT` hands it to the
validator as a lookup; `GET` hands it to `resolve_app`, so `latest` is
resolved on every read.

### Core — `app/core/models.py`

Three groups of pydantic models:

- **Input** (`extra="forbid"`, so a typo'd field is a `422`, not silently
  dropped): `ToolIn`, `ToolEnabledIn`, `ManifestIn` with `AgentIn`,
  `ToolRefIn`, `EscalateRuleIn`, `SupervisorIn`, `BudgetIn`, `RateLimitIn`.
  `EscalateRuleIn.in_` is
  aliased to `in` (a Python keyword) and must be non-empty.
  `SupervisorIn.min_confidence` is optional, 0–1 (ADR-0020); the whole
  `supervisor` block is optional. `ToolRefIn.version` is `x.y.z` or
  `latest` (ADR-0026). `AgentIn.prefetch_tools` (ADR-0028) and
  `AgentIn.budget.daily_tokens` (≥0; 0 stops the agent; omitted →
  `orchestrator`'s default) and `ManifestIn.rate_limit` (`per_minute`,
  `per_source_per_minute`, each ≥1; omitted → `ingestion`'s defaults,
  ADR-0029) are optional. `ManifestIn.stored()` dumps the
  manifest as the JSON document kept in the `apps` row (with `in`, not
  `in_`).
- **Stored**: `Tool` (a `tools` row; `definition()` is the subset `PUT`
  compares to decide `changed`, so `enabled` and timestamps never count),
  `AppRecord` (an `apps` row), `UpsertResult {created, changed}`.
- **Served**: `ResolvedApp` → `ResolvedAgent` → `EffectiveTool`, plus
  `ResolvedToolRef` (each declared tool with the `version` it resolves to,
  `null` if none is registered any more, the `requested_version` as
  written, and its `enabled` flag). An `EffectiveTool.version` is always
  concrete. `supervisor`, `rate_limit`, `prefetch_tools` and `budget` are
  passed through as stored (`null`/empty for manifests registered before
  they existed).

Shared patterns: `SLUG` (`app_id`, `agent_id`, `memory_namespace`),
`TOOL_ID`, `VERSION`, `VERSION_REF` (`VERSION` or `latest`).

### Core — `app/core/validation.py`

`validate_manifest(manifest, url_app_id, lookup_tool) -> list[FieldError]`
is pure: `lookup_tool(tool_id, version) -> Tool | None` is passed in, so
the module has no I/O. Rules, each producing a `FieldError` naming its
field:

| Field | Rejected when |
|---|---|
| `app_id` | differs from the URL's `app_id` |
| `tools[i]` | that `tool_id` + `version` isn't registered (for `latest`: no version is); or it's another app's app-scoped tool |
| `tools[i].scope` | differs from the registered scope |
| `tools[i].tool_id` | declared twice |
| `agents` | not exactly one `role: entry` agent |
| `agents[i].agent_id` | declared twice |
| `agents[i].tool_allowlist[j]` | not declared in the manifest's `tools` |
| `agents[i].invoke_on` | set on the entry agent; empty on a callable agent |
| `agents[i].invoke_on[j]` | not a `Decision` name (`DECISION_UNSPECIFIED` excluded) |
| `agents[i].prefetch_tools[j]` | not on that agent's `tool_allowlist`; or its registered `input_schema` doesn't accept `{}`, since `orchestrator` calls it with no arguments (ADR-0028) |
| `agents[i].prefetch_tools` | a tool repeated |
| `alert_key_fields` | empty, or a field repeated |
| `escalate_when[i].field` | doesn't start with `alert.`, `payload.` or `context.`; an `alert.<name>` whose name isn't in `ENVELOPE_FIELDS` (`source`, `severity`, `message`, `alert_key`; ADR-0010 — `orchestrator`'s guardrails list the same); an invalid `payload.<path>`; or a `context.<path>` that isn't a scalar field of `GetContextResponse` (walked via the proto descriptor, e.g. `context.window_24h.escalation_count` is fine, `context.window_24h` is not) |
| `supervisor.min_confidence` | outside 0–1 (a pydantic `422`, before these checks) |
| `agents[i].budget.daily_tokens`, `rate_limit.*` | negative, or below 1 (pydantic `422`s, likewise) |

Not checked here (files in other images): `prompt_ref` (`orchestrator`),
`event_schema_ref` and whether `alert_key_fields` are properties of it
(`ingestion`).

### Core — `app/core/versions.py` (Day 13, ADR-0026)

`resolve_ref(requested, versions) -> Tool | None`: a pinned version is
that registration or nothing; `latest` is `newest()`, the highest
registered version by number (`version_key`: `1.10.0` > `1.9.0`), enabled
or not — disabling it turns the tool off, it never falls back to an older
one. Resolved on every `GET /apps/{app_id}`, so registering a newer
version moves every `latest` manifest onto it within one reader TTL, with
no re-registration. App tools stay pinned (they ship with their app's
folder); global tools are `latest`.

### Core — `app/core/resolve.py`

`resolve_app(record, tools) -> ResolvedApp`, where `tools` maps each
declared `tool_id` to the registered `Tool` its reference resolves to
(`latest` already resolved). An
agent's `tools` = its `tool_allowlist`, keeping only entries that are
declared, registered, and `enabled`, in allowlist order. A declared tool
that has since been deleted counts as disabled. This one function is what
both `orchestrator` (tool definitions for the LLM) and `tool-gateway`
(per-call allowlist check) end up relying on.

### Storage — `app/db/repository.py`, `app/db/postgres.py`

- **`Repository`** (Protocol): the ten async operations above
  (`list_tool_versions` is every registered version of one tool). Upserts
  return `UpsertResult`; `upsert_tool` never changes `enabled`, so
  re-registering a disabled tool keeps it disabled.
- **`InMemoryRepository`**: dicts keyed by `(tool_id, version)` and
  `app_id`. Used by every unit test.
- **`PostgresRepository`**: asyncpg against the `tools` and `apps` tables
  (`backend/local/postgres/init.sql`). Each upsert runs in a transaction:
  `SELECT … FOR UPDATE`, compare, then `UPDATE` only if different (or
  `INSERT … ON CONFLICT DO NOTHING` for a new row), so two concurrent
  registrations can't both report `changed: false` for different content.
  `create_pool()` registers JSON codecs for `json`/`jsonb`, so manifests
  and schemas round-trip as Python dicts. Covered by
  `tests/integration/test_postgres_repository.py` (needs the Compose
  stack).

### External

- **`GetContextResponse`**, **`Decision`**: proto stubs from `ap-shared`'s
  `proto_gen`, used only by the validator.
- **`Draft202012Validator`** (`jsonschema`): checks a registered tool's
  schemas, and that a prefetched tool accepts `{}`.
- **`observability`**: `ap-shared`'s `setup_observability("registry")`,
  JSON logs to stdout.

## Request flows

**Register an app** (`register_app.py it-ops-triage`):

```mermaid
sequenceDiagram
    participant S as register_app.py
    participant TG as tool-gateway
    participant R as registry
    participant DB as Postgres
    S->>TG: MCP tools/list
    TG-->>S: tools (description, schemas, meta: version, scope, app_id, read_only)
    loop each tool the manifest declares
        Note over S: version "latest" → the version tool-gateway serves;<br/>a pinned version tool-gateway doesn't serve fails the run
        S->>R: PUT /tools/{tool_id}/versions/{version}
        R->>DB: SELECT ... FOR UPDATE, INSERT/UPDATE if different
        R-->>S: {created, changed}
    end
    S->>R: PUT /apps/{app_id} (manifest.yaml as JSON)
    R->>DB: list_tool_versions for each declared tool, resolve_ref
    R->>R: validate_manifest (all errors at once)
    R->>DB: upsert_app (compare, write if different)
    R-->>S: {created, changed} or 422 {errors[]}
```

**Resolve an app** (what `orchestrator`, `ingestion`, `tool-gateway` do
once per TTL per app):

```mermaid
sequenceDiagram
    participant C as reader (RegistryClient, 30s TTL)
    participant R as registry
    participant DB as Postgres
    C->>R: GET /apps/{app_id}
    R->>DB: get_app
    R->>DB: list_tool_versions for each declared tool
    R->>R: resolve_ref (pinned or latest), then resolve_app:<br/>per agent, allowlist ∩ declared ∩ enabled
    R-->>C: ResolvedApp (or 404, also cached one TTL)
```

## Design notes

- **Agents aren't registered on their own.** An agent exists only inside
  one app's manifest (§3), so `PUT /apps` is how agents are registered and
  `GET /agents` lists them from the stored manifests. Their `version` is a
  manifest field, never resolved: there's nothing to resolve it against
  (ADR-0026).
- **Manifest kept whole as jsonb** (`docs/ARCHITECTURE.md` §8): it's read
  whole and written rarely; tools are separate rows because they're shared
  references with their own state (`enabled`) and are checked by key.
- **Resolution happens in `registry`, not in each reader.** Serving each
  agent's effective `tools` means `orchestrator` and `tool-gateway` can't
  drift on what "allowed" means, and a disabled tool needs no manifest
  change.
- **Idempotency is a reported result, not just a property.** `changed:
  false` is what lets `register_app.py` show that re-running an unchanged
  file did nothing (ADR-0006).
- **Strict in, lenient out.** `registry` rejects unknown fields on write;
  readers parse the served shape with `extra="ignore"`, so a field added
  here later doesn't break an older image.
- **`registry` stores limits; others enforce them.** `budget` is
  enforced by `orchestrator`, `rate_limit` by `ingestion`; both read them
  on every manifest fetch, so lowering one takes effect within one TTL.
- **Next changes**: none planned beyond Day 18's `/readyz` (every
  service gets one for Kubernetes).
