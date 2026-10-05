"""orchestrator settings, all from the environment.

The LLM provider's API key is not here: each provider SDK reads its own
variable (`OPENAI_API_KEY`), so the key never passes through platform code.
"""

import os
from dataclasses import dataclass
from pathlib import Path

# backend/services/orchestrator/app/core/config.py -> backend/apps; the
# Dockerfile keeps the same layout under /repo/backend.
DEFAULT_APPS_DIR = Path(__file__).resolve().parents[4] / "apps"


@dataclass(frozen=True)
class Settings:
    apps_dir: Path = DEFAULT_APPS_DIR
    tool_gateway_url: str = "http://localhost:8003/mcp"
    registry_url: str = "http://localhost:8005"
    # memory-store's gRPC GetContext (docs/adr/0019); host port 50053 locally.
    memory_store_target: str = "localhost:50053"
    memory_store_timeout_s: float = 1.0
    # Supervisor threshold when the app's manifest sets none (docs/adr/0020).
    min_confidence: float = 0.6
    # Same default as ingestion and tool-gateway (docs/ARCHITECTURE.md §3).
    manifest_ttl_s: float = 30.0
    kafka_bootstrap_servers: str = "localhost:29092"
    kafka_enabled: bool = True
    grpc_port: int = 50051
    llm_provider: str = "openai"
    llm_model: str = "gpt-5.4-mini"
    # Rounds of LLM tool calls before a run gives up and escalates.
    max_tool_rounds: int = 5
    # Per callable agent run (Day 8); one that takes longer doesn't contribute.
    callable_timeout_s: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            apps_dir=Path(env.get("APPS_DIR", DEFAULT_APPS_DIR)),
            tool_gateway_url=env.get("TOOL_GATEWAY_URL", cls.tool_gateway_url),
            registry_url=env.get("REGISTRY_URL", cls.registry_url),
            memory_store_target=env.get("MEMORY_STORE_TARGET", cls.memory_store_target),
            memory_store_timeout_s=float(env.get("MEMORY_STORE_TIMEOUT_S", cls.memory_store_timeout_s)),
            min_confidence=float(env.get("MIN_CONFIDENCE", cls.min_confidence)),
            manifest_ttl_s=float(env.get("MANIFEST_TTL_S", cls.manifest_ttl_s)),
            kafka_bootstrap_servers=env.get("KAFKA_BOOTSTRAP_SERVERS", cls.kafka_bootstrap_servers),
            kafka_enabled=env.get("KAFKA_ENABLED", "true").lower() == "true",
            grpc_port=int(env.get("GRPC_PORT", cls.grpc_port)),
            llm_provider=env.get("LLM_PROVIDER", cls.llm_provider),
            llm_model=env.get("LLM_MODEL", cls.llm_model),
            max_tool_rounds=int(env.get("MAX_TOOL_ROUNDS", cls.max_tool_rounds)),
            callable_timeout_s=float(env.get("CALLABLE_TIMEOUT_S", cls.callable_timeout_s)),
        )
