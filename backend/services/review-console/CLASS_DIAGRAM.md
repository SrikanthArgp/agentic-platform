# review-console — class diagram

As built through Day 20 of `docs/plan.md` (Day 15 moved verdicts onto
`verdict_outbox`, ADR-0024; Day 18 added `/readyz`, the same as
`/healthz`; cases for all three apps since Day 20, no code change): an `alert.decided` consumer that turns
escalations into cases, the analyst REST API, and the `verdict_outbox`
writer that the `outbox-relay` worker publishes as `verdict.recorded`. Since Day 13 `GET /cases` has a second
reader: `tool-gateway`'s global `similar-past-case-lookup` asks it for
`status=RESOLVED` cases on the run's `app_id` + `alert_key` (ADR-0027).

```mermaid
classDiagram
    direction LR
    class main {
        <<module: app.main>>
        +create_app(settings, store) FastAPI
        +healthz() HealthResponse
        +readyz() HealthResponse
    }
    class Settings {
        <<dataclass: app.core.config>>
        +postgres_dsn: str
        +kafka_bootstrap_servers: str
        +kafka_enabled: bool
        +from_env() Settings
    }
    class cases_api {
        <<router: app.api.cases>>
        +GET /cases?app_id&alert_key&status&limit&offset
        +GET /cases/id
        +POST /cases/id/verdict
    }
    class CaseService {
        <<app.core.service>>
        +record_decision(RunAgentResponse) bool?
        +get(case_id) Case
        +list(CaseQuery) list~Case~
        +record_verdict(case_id, VerdictIn) Case
    }
    class CaseStore {
        <<protocol>>
        +add(NewCase) bool
        +get(case_id) Case?
        +list(CaseQuery) list~Case~
        +lock(case_id) LockedCase
    }
    class PostgresCaseStore {
        <<app.db.postgres>>
    }
    class LockedCase {
        <<protocol>>
        +case: Case?
        +resolve(VerdictIn, at) Case
        +enqueue(VerdictOutboxRow) None
    }
    class VerdictOutboxRow {
        <<dataclass: app.core.outbox>>
        +app_id, topic, kafka_key
        +case_id: int
        +payload: bytes
        +headers: dict
    }
    class StoreUnavailableError {
        <<exception: app.core.outbox>>
    }
    class DecisionConsumer {
        <<app.kafka.decisions>>
        group = review-console
        +start()
        +stop()
    }
    class NewCase {
        <<pydantic: app.core.cases>>
        +app_id, alert_id, alert_key, agent_id
        +decision: str
        +confidence: float
        +reasons: list~str~
        +tool_calls: list~ToolCallSummary~
        +alert: AlertSummary?
    }
    class AlertSummary {
        <<pydantic>>
        +source, severity, message
        +fired_at: datetime?
        +payload: dict
    }
    class ToolCallSummary {
        <<pydantic>>
        +tool_name, result_summary
        +agent_id: str
    }
    class Case {
        <<pydantic>>
        +id: int
        +status: OPEN or RESOLVED
        +verdict: Verdict?
        +verdict_by: str?
        +resolution_notes: str?
        +created_at, resolved_at?
    }
    class VerdictIn {
        <<pydantic>>
        +verdict: CONFIRMED_INCIDENT or CONFIRMED_NOISE
        +verdict_by: str
        +resolution_notes: str?
    }

    main ..> Settings
    main ..> cases_api
    main ..> DecisionConsumer : started with the app
    cases_api ..> CaseService
    DecisionConsumer ..> CaseService : handle_decided()
    CaseService ..> CaseStore
    CaseService ..> VerdictOutboxRow : verdict.recorded event
    PostgresCaseStore ..|> CaseStore
    CaseStore ..> LockedCase : lock()
    LockedCase ..> VerdictOutboxRow : same transaction
    PostgresCaseStore ..> StoreUnavailableError : connection failures
    NewCase <|-- Case
    NewCase *-- AlertSummary
    NewCase *-- ToolCallSummary
    CaseService ..> VerdictIn
```

- **`case_from_decision()`** (`app.core.cases`) is the persistence filter:
  only `ESCALATE` becomes a `NewCase`; `AUTO_RESOLVE`/`SUPPRESS` return
  `None`. It copies `RunAgentResponse.alert` into `AlertSummary` (`None`
  for a decision published before ADR-0023) and each `ToolCall`'s
  `agent_id`. `PostgresCaseStore.add` is `ON CONFLICT (app_id, alert_id) DO
  NOTHING`, so a redelivered decision never creates a second case.
- **`CaseService.record_verdict`** holds the state machine, under
  `lock()` (`SELECT ... FOR UPDATE`): no case → `CaseNotFoundError` (404),
  `RESOLVED` → `CaseAlreadyResolvedError` (409), else `resolve()` and
  `enqueue()` the `verdict.recorded` event into `verdict_outbox`, in the
  same transaction (ADR-0024). Both commit or neither does. Kafka isn't on
  this path: a verdict succeeds during an outage and the `outbox-relay`
  worker publishes it later, timestamped by the row's `created_at`. Only
  a Postgres connection failure is `StoreUnavailableError` (503, nothing
  committed). This closes the Day 11 gap where a timed-out publish could
  reach `memory-store` for a case that stayed `OPEN`.
- **`verdict_event()`** builds the `verdict.recorded` body: `{app_id,
  case_id, alert_key, verdict, verdict_by, recorded_at}`, keyed
  `{app_id}:{alert_key}`. `resolution_notes` stays on the case (ADR-0011).
- **`DecisionConsumer`**: own group, one message at a time, commit after
  handling; unusable messages are skipped, Postgres errors retried.
- With `KAFKA_ENABLED=false`, `main` doesn't start the consumer; cases and
  verdicts work as usual (verdict rows wait in `verdict_outbox`).
