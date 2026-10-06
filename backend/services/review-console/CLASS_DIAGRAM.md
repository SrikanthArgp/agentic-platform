# review-console — class diagram

As built through Day 11 of `docs/plan.md` (unchanged since Day 9): an `alert.decided` consumer that
turns escalations into cases, the analyst REST API, and the
`verdict.recorded` producer.

```mermaid
classDiagram
    direction LR
    class main {
        <<module: app.main>>
        +create_app(settings, store, publisher) FastAPI
        +healthz() HealthResponse
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
    }
    class DecisionConsumer {
        <<app.kafka.decisions>>
        group = review-console
        +start()
        +stop()
    }
    class Publisher {
        <<protocol: app.kafka.publisher>>
        +publish(topic, key, value)
    }
    class KafkaPublisher
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
    CaseService ..> Publisher : verdict.recorded
    PostgresCaseStore ..|> CaseStore
    CaseStore ..> LockedCase : lock()
    KafkaPublisher ..|> Publisher
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
  publish `verdict.recorded` before commit. A `PublishError` rolls back
  (503; the case stays `OPEN`). A 503 from the send *timeout* may still be
  delivered once Kafka is back (Day 11; open decision in `plan.md` Day 15).
- **`verdict_event()`** builds the `verdict.recorded` body: `{app_id,
  case_id, alert_key, verdict, verdict_by, recorded_at}`, keyed
  `{app_id}:{alert_key}`. `resolution_notes` stays on the case (ADR-0011).
- **`DecisionConsumer`**: own group, one message at a time, commit after
  handling; unusable messages are skipped, Postgres errors retried.
- With `KAFKA_ENABLED=false`, `main` uses a publisher that always raises
  `PublishError`: cases are readable, verdicts get 503.
