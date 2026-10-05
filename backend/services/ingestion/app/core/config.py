"""ingestion settings, all from the environment."""

import os
from dataclasses import dataclass
from pathlib import Path

# backend/services/ingestion/app/core/config.py -> backend/apps; the
# Dockerfile keeps the same layout under /repo/backend.
DEFAULT_APPS_DIR = Path(__file__).resolve().parents[4] / "apps"


@dataclass(frozen=True)
class Settings:
    apps_dir: Path = DEFAULT_APPS_DIR
    kafka_bootstrap_servers: str = "localhost:29092"
    kafka_enabled: bool = True
    registry_url: str = "http://localhost:8005"
    # Same default as orchestrator and tool-gateway (docs/ARCHITECTURE.md §3).
    manifest_ttl_s: float = 30.0
    # Serialized payload limit: everything in it ends up in an LLM prompt.
    max_payload_bytes: int = 32 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            apps_dir=Path(env.get("APPS_DIR", DEFAULT_APPS_DIR)),
            kafka_bootstrap_servers=env.get("KAFKA_BOOTSTRAP_SERVERS", cls.kafka_bootstrap_servers),
            kafka_enabled=env.get("KAFKA_ENABLED", "true").lower() == "true",
            registry_url=env.get("REGISTRY_URL", cls.registry_url),
            manifest_ttl_s=float(env.get("MANIFEST_TTL_S", cls.manifest_ttl_s)),
            max_payload_bytes=int(env.get("MAX_PAYLOAD_BYTES", cls.max_payload_bytes)),
        )
