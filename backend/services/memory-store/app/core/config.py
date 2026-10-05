"""memory-store settings, all from the environment."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    postgres_dsn: str = "postgresql://platform:platform@localhost:5432/platform"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:29092"
    kafka_enabled: bool = True
    registry_url: str = "http://localhost:8005"
    # Same default as ingestion, orchestrator and tool-gateway (ARCHITECTURE §3).
    manifest_ttl_s: float = 30.0
    grpc_port: int = 50051

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            postgres_dsn=env.get("POSTGRES_DSN", cls.postgres_dsn),
            redis_url=env.get("REDIS_URL", cls.redis_url),
            kafka_bootstrap_servers=env.get("KAFKA_BOOTSTRAP_SERVERS", cls.kafka_bootstrap_servers),
            kafka_enabled=env.get("KAFKA_ENABLED", "true").lower() == "true",
            registry_url=env.get("REGISTRY_URL", cls.registry_url),
            manifest_ttl_s=float(env.get("MANIFEST_TTL_S", cls.manifest_ttl_s)),
            grpc_port=int(env.get("GRPC_PORT", cls.grpc_port)),
        )
