# outbox-relay — class diagram

As built through Day 20 of `docs/plan.md` (ADR-0030, ADR-0024; Day 16
added attempts, `DEAD` rows and the DLQ; unchanged since, and on the kind
cluster (Day 18) one replica pinned to the control-plane, ADR-0032). A worker, not a
platform service: it drains `ingestion`'s `outbox` and `review-console`'s
`verdict_outbox` to Kafka, one active instance at a time. Its only HTTP
endpoints are `/healthz` and `/readyz`.

```mermaid
classDiagram
    direction LR
    class main {
        <<module: app.main>>
        +create_app(settings, relay, start) FastAPI
        +healthz() HealthResponse
        +readyz() ReadyResponse
    }
    class Settings {
        <<dataclass: app.config>>
        +postgres_dsn, kafka_bootstrap_servers
        +poll_interval_s = 0.5
        +batch_size = 100
        +max_attempts = 5
        +retention_h = 24
        +prune_interval_s = 300
        +lock_key
    }
    class Relay {
        <<app.relay>>
        +role: Role
        +run()
        +run_session(Session)
        +drain_once(Session) bool
        +drain_table(Session, OutboxTable) int
        +prune_if_due(Session)
    }
    class OutboxTable {
        <<frozen dataclass>>
        +name: str
        +timestamp_from_created_at: bool
        +dlq_topic: str?
    }
    class Role {
        <<enum>>
        starting, disconnected, standby, active
    }
    class Database {
        <<protocol>>
        +connect() Session
    }
    class Session {
        <<protocol>>
        +try_lock() bool
        +claim(table, limit) Claim
        +prune(table, retention_h) int
        +close()
    }
    class Claim {
        <<protocol>>
        +rows: list~OutboxRow~
        +mark_sent(ids)
        +mark_failed(id, error, attempts)
        +mark_dead(id, error, attempts)
    }
    class Producer {
        <<protocol>>
        +send(topic, key, value, headers, timestamp_ms)
    }
    class MessageRejected {
        <<exception: this message, counts an attempt>>
    }
    class KafkaUnavailable {
        <<exception: Kafka, no attempt>>
    }
    class PostgresDatabase {
        <<app.postgres>>
    }
    class PostgresSession {
        <<app.postgres: one asyncpg connection>>
    }
    class KafkaProducer {
        <<app.kafka: idempotent, acks=all>>
    }

    main ..> Settings
    main *-- Relay : run() as a task
    main *-- KafkaProducer
    Relay --> Database
    Relay --> Producer
    Relay ..> OutboxTable : outbox, verdict_outbox
    Relay --> Role
    Database ..> Session : connect()
    Session ..> Claim : claim()
    PostgresDatabase ..|> Database
    PostgresSession ..|> Session
    KafkaProducer ..|> Producer
    KafkaProducer ..> MessageRejected : too large, corrupt, invalid topic
    KafkaProducer ..> KafkaUnavailable : everything else
```

- **One active relay**: a `Session` is one Postgres connection. The relay
  drains only after `try_lock()` (`pg_try_advisory_lock`) succeeds on it,
  and the lock is released when that connection closes or drops. Any
  connection error is `DatabaseUnavailable`: the session ends, the role
  becomes `disconnected`, and `run()` reconnects and competes for the lock
  again. A second instance sits in `standby`.
- **`drain_table`**: claims up to `batch_size` `PENDING` rows in `id` order
  (`FOR UPDATE SKIP LOCKED`), sends them one by one with the stored
  `kafka_key` and headers, and marks the sent ones `SENT` in the same
  transaction. A `KafkaUnavailable` stops the batch at that row. Rows
  already sent are committed, the rest stay `PENDING` with no attempt
  counted, and `run_session` backs off. A `MessageRejected` also stops the
  batch, after `mark_failed` (`attempts + 1`, `last_error`). At
  `max_attempts` the row is instead `mark_dead`, copied to its table's
  `dlq_topic` by `_dead_letter` (headers only, `payload_dropped`, if the
  DLQ refuses the payload too), and the batch continues. A full batch
  loops straight away; otherwise it sleeps `poll_interval_s`.
- **Sessions never kill the worker**: `DatabaseUnavailable`, or anything
  unexpected (e.g. asyncpg's `InternalClientError` when Postgres kills
  the connection mid-query), ends the session; `run()` closes it,
  reconnects and competes for the lock again.
- **Timestamps**: `verdict_outbox` rows are sent with `created_at` as the
  Kafka timestamp, because `memory-store` times a verdict by it.
- **Pruning**: `prune_if_due` deletes `SENT` rows older than
  `retention_h` from both tables, every `prune_interval_s`, and only on the
  active relay. `DEAD` rows are kept.
- **Replay**: `backend/scripts/replay_dead.py` sets a `DEAD` row back to
  `PENDING` with `attempts = 0`.
