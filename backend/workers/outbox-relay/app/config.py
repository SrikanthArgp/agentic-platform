"""outbox-relay settings, all from the environment (ADR-0030)."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    postgres_dsn: str = "postgresql://platform:platform@localhost:5432/platform"
    kafka_bootstrap_servers: str = "localhost:29092"
    # Idle wait between empty polls; a full batch loops straight away.
    poll_interval_s: float = 0.5
    batch_size: int = 100
    # Rejections of one message before its row is DEAD (Day 16).
    max_attempts: int = 5
    # SENT rows older than this are deleted, checked every prune_interval_s.
    retention_h: float = 24.0
    prune_interval_s: float = 300.0
    # The session advisory lock that makes one relay the active one.
    lock_key: int = 7_301_530

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            postgres_dsn=env.get("POSTGRES_DSN", cls.postgres_dsn),
            kafka_bootstrap_servers=env.get("KAFKA_BOOTSTRAP_SERVERS", cls.kafka_bootstrap_servers),
            poll_interval_s=float(env.get("RELAY_POLL_INTERVAL_S", cls.poll_interval_s)),
            batch_size=int(env.get("RELAY_BATCH_SIZE", cls.batch_size)),
            max_attempts=int(env.get("RELAY_MAX_ATTEMPTS", cls.max_attempts)),
            retention_h=float(env.get("OUTBOX_RETENTION_H", cls.retention_h)),
            prune_interval_s=float(env.get("OUTBOX_PRUNE_INTERVAL_S", cls.prune_interval_s)),
            lock_key=int(env.get("RELAY_LOCK_KEY", cls.lock_key)),
        )
