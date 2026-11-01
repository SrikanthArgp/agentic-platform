# ingestion — class diagram

As built through Day 15 of `docs/plan.md` (Day 15 replaced the Kafka
publisher with the Postgres outbox, ADR-0030; Day 14 added rate limits per
app and per source). `ingestion` is the platform's
REST front door: it accepts an alert at `POST /apps/{app_id}/events`,
checks the app's rate limits, validates it against that app's rules (read
from `registry`), turns it into a `RunAgentRequest`, and writes it to the
`outbox` table; the `outbox-relay` worker publishes it to `alert.received`. It never runs an agent and never decides anything.

Python modules that are plain functions (no class) are drawn as
`<<module>>` boxes, so every piece of behaviour shows up somewhere.
Third-party and cross-service types are in the `external` group.

```mermaid
classDiagram
    direction LR

    namespace entrypoint {
        class main {
            <<module>>
            +SERVICE_NAME = "ingestion"
            +create_app(settings, outbox, apps, rate_limiter) FastAPI
            -lifespan(app) AsyncIterator
            +healthz() HealthResponse
            +readyz() HealthResponse | NotReadyResponse
        }
        class HealthResponse {
            <<pydantic>>
            +status: str
            +service: str
        }
        class NotReadyResponse {
            <<pydantic, 503>>
            +reasons: list~str~
        }
    }

    namespace api {
        class alerts_router {
            <<module: app.api.alerts>>
            +post_event(alert: AlertIn, request, app_id) AlertAccepted
        }
    }

    namespace core {
        class Settings {
            <<frozen dataclass>>
            +apps_dir: Path
            +postgres_dsn: str
            +registry_url: str
            +manifest_ttl_s: float
            +max_payload_bytes: int
            +redis_url: str?
            +rate_limit_per_minute: int
            +rate_limit_per_source_per_minute: int
            +from_env()$ Settings
        }
        class AppStore {
            -_registry: AppSource
            -_apps_dir: Path
            -_specs: dict~tuple, AppEventSpec~
            +get(app_id) AppEventSpec
            -_load_spec(app_id, schema_ref, fields) AppEventSpec
        }
        class AppSource {
            <<Protocol>>
            +get_app(app_id) dict
        }
        class AppEventSpec {
            <<frozen dataclass>>
            +app_id: str
            +alert_key_fields: tuple~str~
            +validator: Draft202012Validator
            +limits: Limits
            +payload_errors(payload) list~dict~
        }
        class RateLimiter {
            -_redis: Redis
            -_default_per_minute: int
            -_default_per_source: int
            +hit(app_id, source, limits) None
        }
        class Limits {
            <<frozen dataclass>>
            +per_minute: int?
            +per_source_per_minute: int?
            +from_manifest(rate_limit)$ Limits
        }
        class RateLimited {
            <<exception>>
            +scope: str
            +limit: int
            +retry_after_s: int
        }
        class envelope {
            <<module: app.core.envelope>>
            +build_alert_key(payload, fields) str
            +message_key(app_id, alert_key) str
            +build_request(app_id, alert_id, alert_key, alert, received_at) RunAgentRequest
        }
        class UnknownAppError {
            <<exception: LookupError>>
        }
        class AppConfigError {
            <<exception: ValueError>>
        }
        class AppUnavailableError {
            <<exception: RuntimeError>>
        }
        class AlertKeyError {
            <<exception: ValueError>>
        }
    }

    namespace models {
        class AlertIn {
            <<pydantic, extra=forbid>>
            +source: str
            +severity: str
            +message: str
            +timestamp: AwareDatetime?
            +payload: dict~str, Any~
        }
        class AlertAccepted {
            <<pydantic>>
            +alert_id: str
            +app_id: str
            +alert_key: str
            +status = "accepted"
        }
    }

    namespace outbox {
        class Outbox {
            <<Protocol>>
            +write(entry: OutboxEntry) None
            +ping() str?
        }
        class PostgresOutbox {
            -_dsn: str
            -_pool: asyncpg.Pool?
            +start() None
            +stop() None
            +write(entry: OutboxEntry) None
        }
        class OutboxEntry {
            <<frozen dataclass>>
            +app_id: str
            +alert_id: str
            +topic: str
            +kafka_key: str
            +payload: bytes
            +headers: dict~str, str~
        }
        class OutboxError {
            <<exception: RuntimeError>>
        }
        class writer {
            <<module: app.outbox.writer>>
            +trace_headers() dict~str, str~
        }
    }

    namespace external {
        class RunAgentRequest {
            <<proto: agent.proto>>
        }
        class asyncpg {
            <<asyncpg>>
        }
        class Redis {
            <<redis.asyncio>>
        }
        class Draft202012Validator {
            <<jsonschema>>
        }
        class observability {
            <<ap-shared>>
            +setup_observability(service_name) Tracer
        }
        class Lifecycle {
            <<ap-shared lifecycle>>
            +started: bool
            +draining: bool
            +install_sigterm_hook() None
            +mark_started() None
            +begin_drain() None
            +not_ready_reasons(checks) list~str~
        }
        class RegistryClient {
            <<ap-shared registry_client>>
            +get_app(app_id) dict
            +aclose() None
        }
        class AppNotFoundError {
            <<ap-shared registry_client>>
        }
        class RegistryUnavailableError {
            <<ap-shared registry_client>>
        }
    }

    %% wiring done by create_app()
    main ..> Settings : reads from_env
    main *-- AppStore : app.state.apps
    main *-- RegistryClient : unless apps injected, closed in lifespan
    main *-- PostgresOutbox : app.state.outbox, unless injected
    main *-- RateLimiter : app.state.rate_limiter, if REDIS_URL or injected
    main ..> observability : setup at import
    main *-- Lifecycle : app.state.lifecycle; /readyz, SIGTERM hook
    main ..> Outbox : /readyz calls ping()
    main ..> alerts_router : include_router
    main ..> HealthResponse

    %% request path
    alerts_router ..> AlertIn : request body
    alerts_router ..> AlertAccepted : 202 response
    alerts_router ..> AppStore : get(app_id)
    alerts_router ..> RateLimiter : hit(app_id, source, spec.limits)
    alerts_router ..> RateLimited : 429 + Retry-After
    alerts_router ..> envelope : alert_key, request, kafka_key
    alerts_router ..> Outbox : write alert.received row
    alerts_router ..> writer : trace_headers()
    alerts_router ..> UnknownAppError : 404
    alerts_router ..> AppUnavailableError : 503
    alerts_router ..> AppConfigError : 500
    alerts_router ..> AlertKeyError : 422
    alerts_router ..> OutboxError : 503

    %% core
    AppStore --> AppSource
    RegistryClient ..|> AppSource
    AppStore o-- AppEventSpec : caches per (app_id, schema_ref, key fields)
    AppStore ..> AppNotFoundError : catches
    AppStore ..> RegistryUnavailableError : catches
    AppStore ..> UnknownAppError : raises
    AppStore ..> AppUnavailableError : raises
    AppStore ..> AppConfigError : raises
    AppEventSpec *-- Draft202012Validator
    AppEventSpec *-- Limits : from manifest rate_limit, every fetch
    RateLimiter *-- Redis : 0.2s timeouts
    RateLimiter ..> Limits
    RateLimiter ..> RateLimited : raises
    envelope ..> AlertIn : reads
    envelope ..> RunAgentRequest : builds
    envelope ..> AlertKeyError : raises

    %% kafka
    KafkaPublisher ..|> Publisher : implements
    KafkaPublisher *-- AIOKafkaProducer : idempotent, acks=all
    KafkaPublisher ..> PublishError : raises
```

## Classes and modules

### Entry point — `app/main.py`

**`main` (module)** is the composition root. `create_app()` builds every
collaborator and stores them on `app.state` so the router can reach them
without globals:

| `app.state` key | Object | Notes |
|---|---|---|
| `settings` | `Settings` | |
| `apps` | `AppStore` | over the `RegistryClient` (or a fake a test passes as `apps`) |
| `outbox` | `Outbox` | the `PostgresOutbox`, or the fake a test passes in |
| `lifecycle` | `Lifecycle` (ap-shared) | started/draining, for `/readyz` |
| `rate_limiter` | `RateLimiter` or `None` | over Redis when `REDIS_URL` is set (0.2s socket timeouts), or the one a test passes in; `None` (a startup warning) means no rate limiting |

`lifespan()` starts the `PostgresOutbox` pool (warning, not failing, if
Postgres is down), wraps uvicorn's SIGTERM handler
(`Lifecycle.install_sigterm_hook()`) and marks the app started. On
SIGTERM `/readyz` fails at once; uvicorn closes its listener and lets
in-flight requests (each at most one outbox write) finish; then the
lifespan shutdown closes the pool, the `RegistryClient` and Redis client
if `create_app()` made them (Day 19). `setup_observability()` is called at import
time so logging is JSON from the first line.

**`HealthResponse`** is the `/healthz` body (`status`, `service`). Compose
health checks and the Kubernetes liveness probe use it; it never checks a
dependency, so an outage takes pods out of rotation without restarting
them. `/readyz` (the Kubernetes readiness probe, Day 19) answers the same
body when the app has started, isn't draining and `outbox.ping()` (a
`SELECT 1`, 1s) succeeds; otherwise `503` with a **`NotReadyResponse`**
naming why (`starting`, `draining`, `outbox: Postgres unreachable (…)`).

### API — `app/api/alerts.py`

**`alerts_router` (module)** has the two HTTP endpoints.

- **`post_event`**, `POST /apps/{app_id}/events`: the intake. The URL
  chooses the app; nothing infers it. Everything is validated before Kafka
  is touched, in this order:
  1. The envelope, by pydantic through `AlertIn`. Failure is a `422`.
  2. The serialized payload size against `max_payload_bytes`. Failure is a `413`.
  3. The app's spec, from `AppStore`. An unregistered app is a `404`;
     `registry` unreachable with nothing cached is a `503` with
     `Retry-After: 5`; a broken app (`AppConfigError`) is a `500`.
  4. The app's rate limits (Day 14), through `RateLimiter.hit`. Over either
     is a `429` with `Retry-After` (seconds to the next minute) and a body
     naming the limit (`limit`, `limit_per_minute`). Checked after the app
     resolves, so an unknown app is still a `404`.
  5. The payload against the app's JSON Schema. Failure is a `422` listing
     every error.
  6. `alert_key`, built by `build_alert_key`. A missing field is a `422`.

  It then mints `alert_id`, builds the protobuf and publishes. A
  `PublishError` becomes a `503` with `Retry-After: 5`.

Day 4's `POST /alerts` and its debug `GET /alerts/{id}` are gone (both
`404`). `ingestion` reads nothing back: a decision is seen on
`review-console` (escalations, as cases) or on the `alert.decided` topic.

### Core — `app/core/`

**`Settings`** (`config.py`): frozen dataclass of everything configurable.
`from_env()` reads `APPS_DIR`, `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_ENABLED`,
`REGISTRY_URL` (default `http://localhost:8005`), `MANIFEST_TTL_S`
(default 30, the same as `orchestrator` and `tool-gateway`),
`MAX_PAYLOAD_BYTES`, `REDIS_URL` (unset: no rate limiting),
`RATE_LIMIT_PER_MINUTE` (default 600) and
`RATE_LIMIT_PER_SOURCE_PER_MINUTE` (default 300). `apps_dir` defaults to
`backend/apps`, a path that is the same in the repo and in the Docker image.

**`AppStore`** (`apps.py`): resolves `app_id` to an `AppEventSpec`.
- It asks its **`AppSource`** (the `ap-shared` `RegistryClient` in
  production, a fake in tests) for the app: `GET /apps/{app_id}` behind the
  shared 30s TTL cache. The client rejects a non-slug `app_id` before it
  becomes a URL, and caches 404s too, so a newly registered app is
  accepted within one TTL.
- From the manifest it reads `event_schema_ref` and `alert_key_fields`; the
  schema itself is a file in this image (`APPS_DIR/{app_id}/`).
- It keeps one compiled spec per `(app_id, event_schema_ref,
  alert_key_fields)`, so a manifest change builds a new spec on the next
  fetch, with no restart. The manifest's `rate_limit` isn't part of that
  key: it's read into the returned spec's `limits` on every fetch, so a
  limit change lands within one TTL.
- `AppNotFoundError` becomes `UnknownAppError` (`404`);
  `RegistryUnavailableError` becomes `AppUnavailableError` (`503`); a broken
  app raises `AppConfigError` (`500`).

The split matters: an unknown app is the caller's mistake (4xx), registry
being down is temporary (503, retry), and a broken app is a platform bug
(5xx).

**`AppEventSpec`** (`apps.py`): what `ingestion` needs from an app's
manifest, and nothing else:
- `alert_key_fields`, in manifest order;
- a compiled `Draft202012Validator` for the app's `event_schema_ref`.

`payload_errors()` returns every schema violation as `{path, message}`,
sorted so error responses are deterministic.

**`RateLimiter`** (`ratelimit.py`, Day 14, ADR-0029): fixed one-minute
windows, two Redis counters per event, incremented together (with a
2-minute `EXPIRE`) in one transaction:
- `ratelimit:{app_id}:{minute}`: every event posted for the app;
- `ratelimit:{app_id}:src:{source}:{minute}`: those with that envelope
  `source`.

`hit()` raises **`RateLimited`** (`scope` `per_minute` or
`per_source_per_minute`, the `limit`, `retry_after_s`) when either is over
its limit: the app's **`Limits`** (manifest `rate_limit`) or the
`Settings` defaults. Refused events count too, and so do events that will
fail payload validation, so retrying faster earns no fresh allowance. Each
app has its own counters, so one app's flood never takes another's
allowance. `source` is unauthenticated (§13 T6): a sender can dodge its
per-source limit by varying it, never the app's. Redis unreachable → the
event is accepted and a warning logged: a rate limit protects capacity,
it isn't a correctness check.

**`AppStore._load_spec`** builds the spec. These are the checks `registry`
can't do, because the schema file isn't in its image (ARCHITECTURE §12). It
rejects, with `AppConfigError`:
- a manifest without `event_schema_ref` or with empty `alert_key_fields`;
- a schema path that resolves outside the app's folder, or no such file;
- an invalid JSON Schema;
- an `alert_key_fields` entry that isn't a property of the schema.

**`envelope` (module)** is the pure transformation layer, with no I/O:
- `build_alert_key()` joins the named payload fields with `:`, in manifest
  order. Booleans are lower-cased; strings and numbers are used as-is.
  Missing, empty, or object/array values raise `AlertKeyError`. Because the
  key is built here and never by the agent, the same event always maps to
  the same memory key and Kafka partition (ADR-0007).
- `message_key()` returns the Kafka key `{app_id}:{alert_key}` (ADR-0002),
  stored as the outbox row's `kafka_key`.
  Every topic uses it, so all messages for one alert stay in order on one
  partition.
- `build_request()` builds the `RunAgentRequest` from the envelope fields
  plus `payload`, which becomes a `google.protobuf.Struct` (numbers become
  doubles). `timestamp` defaults to when `ingestion` received the alert.

**Exceptions**: `UnknownAppError`, `AppUnavailableError`, `AppConfigError`,
`RateLimited` and `AlertKeyError` are small domain exceptions. The router maps each to a different HTTP status.

### Models — `app/models/alerts.py`

**`AlertIn`**: the request body. It holds the platform-generic envelope
(`source`, `severity`, `message`, optional timezone-aware `timestamp`) plus
the app-specific `payload`. `extra="forbid"` means a caller can't sneak in
`alert_id`, `alert_key` or `app_id`: `app_id` comes from the URL, the other
two are set by `ingestion`.

**`AlertAccepted`**: the `202` body. It echoes the ids `ingestion`
generated, so the caller can find the decision later (by `alert_id` /
`alert_key` on `review-console`'s `GET /cases`, if it escalated).

### Outbox — `app/outbox/writer.py`

**`Outbox`** (Protocol): the one-method interface the router depends on.
Tests inject a fake; production uses `PostgresOutbox`.

**`PostgresOutbox`**: an asyncpg pool. `start()` creates it but only warns
if Postgres is down, so the service comes up anyway; `write()` retries the
pool and inserts one `outbox` row (5s timeout), raising `OutboxError` on
any connection or database failure. `ping()` (for `/readyz`) runs
`SELECT 1` with a 1s timeout and returns why it failed, or `None`.

**`OutboxEntry`** is the row: topic `alert.received`, `kafka_key`, the
serialized `RunAgentRequest`, and `headers` from **`trace_headers()`**, the
W3C trace context of the request's span, which the relay forwards as Kafka
headers.

An alert therefore gets a `202` once its row is committed, whether or not
Kafka is up. `ingestion` has no Kafka client at all (ADR-0030).

## Request flow (`POST /apps/{app_id}/events`)

1. FastAPI parses the body into **`AlertIn`**.
2. **`alerts_router.post_event`** checks the size, then calls
   **`AppStore.get`**, which gets the app from **`RegistryClient`**
   (cached 30s). For a new manifest version, `_load_spec` builds an
   **`AppEventSpec`**.
3. **`RateLimiter.hit`** counts the event against the app's and the
   source's limits (`429` if over).
4. **`AppEventSpec.payload_errors`** validates the payload, and
   **`envelope.build_alert_key`** builds `alert_key`.
5. **`envelope.build_request`** builds the **`RunAgentRequest`**.
6. **`Outbox.write`** inserts it as an `outbox` row keyed by
   **`envelope.message_key`**. The caller gets **`AlertAccepted`** (`202`).
7. The `outbox-relay` worker publishes the row to `alert.received` (up to
   one 0.5s poll later); later `orchestrator` publishes to `alert.decided`.
   `ingestion` isn't involved in either.

## Design notes

- **Dependency injection through `create_app()`**: tests pass `Settings`,
  a fake `Outbox`, a fake registry (`apps`) and, when they test limits,
  a `RateLimiter` over fake Redis, so no unit test needs Postgres, Redis or
  `registry`.
- **Nothing app-specific in code**: every per-app rule comes from the
  app's manifest in `registry` (key fields, which schema) and its files
  under `backend/apps/{app_id}/` (the schema itself).
- **Fail before writing**: all validation happens before the outbox
  write, so a bad alert never reaches the agent.
- **Graceful stop** (Day 19): readiness fails on SIGTERM and in-flight
  requests finish (`tests/test_shutdown.py`, against a real uvicorn).
