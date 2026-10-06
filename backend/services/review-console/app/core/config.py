"""review-console settings, all from the environment."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    postgres_dsn: str = "postgresql://platform:platform@localhost:5432/platform"
    kafka_bootstrap_servers: str = "localhost:29092"
    kafka_enabled: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            postgres_dsn=env.get("POSTGRES_DSN", cls.postgres_dsn),
            kafka_bootstrap_servers=env.get("KAFKA_BOOTSTRAP_SERVERS", cls.kafka_bootstrap_servers),
            kafka_enabled=env.get("KAFKA_ENABLED", "true").lower() == "true",
        )
