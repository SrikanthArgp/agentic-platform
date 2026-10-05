"""App Manifest resolution: `app_id` -> manifest, agent, prompt.

The manifest comes from `registry` (`GET /apps/{app_id}`, via `ap-shared`'s
`RegistryClient` and its 30s TTL cache, docs/ARCHITECTURE.md §3), already
resolved: each agent carries the tools it may call right now (allowlisted,
declared, enabled), which is exactly what the agent is offered. Prompts stay
files in this image (`prompt_ref` is resolved here, §12).
"""

from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agent.guardrails import Rule

from registry_client import AppNotFoundError, RegistryUnavailableError


class ResolutionError(LookupError):
    """The run names an app or agent that doesn't exist, or the app is broken."""


class ManifestUnavailableError(RuntimeError):
    """`registry` couldn't be reached and there is no cached copy of the app."""


class AgentTool(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool_id: str
    version: str
    scope: Literal["app", "global"]
    description: str
    input_schema: dict[str, Any]


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: str
    version: str
    role: Literal["entry", "callable"]
    prompt_ref: str
    tool_allowlist: list[str] = []
    invoke_on: list[str] = []
    # The tools this agent is offered: computed by registry (§3).
    tools: list[AgentTool] = []


class EscalateRule(BaseModel):
    """One `escalate_when` guardrail (ADR-0010, ADR-0021); registry validated its field."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    field: str
    in_: list[str | bool | int | float] = Field(alias="in")

    def rule(self) -> Rule:
        return Rule(self.field, self.in_)


class SupervisorSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # None: orchestrator's MIN_CONFIDENCE default (docs/adr/0020).
    min_confidence: float | None = Field(default=None, ge=0, le=1)


class AppManifest(BaseModel):
    # extra="ignore": fields registry adds later must not break an older
    # orchestrator.
    model_config = ConfigDict(extra="ignore")

    app_id: str
    display_name: str
    agents: list[AgentSpec]
    memory_namespace: str = ""
    escalate_when: list[EscalateRule] = []
    supervisor: SupervisorSpec | None = None

    def guardrails(self) -> list[Rule]:
        return [r.rule() for r in self.escalate_when]

    def min_confidence(self, default: float) -> float:
        value = self.supervisor.min_confidence if self.supervisor else None
        return default if value is None else value

    def agent(self, agent_id: str) -> AgentSpec:
        """The named agent, or the app's entry agent when `agent_id` is empty."""
        for agent in self.agents:
            if (agent.agent_id == agent_id) if agent_id else (agent.role == "entry"):
                return agent
        wanted = repr(agent_id) if agent_id else "an entry agent"
        raise ResolutionError(f"App '{self.app_id}' has no agent {wanted}.")


class AppSource(Protocol):
    """`registry_client.RegistryClient`, or a fake in tests."""

    async def get_app(self, app_id: str) -> dict[str, Any]: ...


class ManifestStore:
    def __init__(self, registry: AppSource, apps_dir: Path):
        self._registry = registry
        self._apps_dir = apps_dir.resolve()

    async def get(self, app_id: str) -> AppManifest:
        try:
            app = await self._registry.get_app(app_id)
        except AppNotFoundError as e:
            raise ResolutionError(str(e)) from None
        except RegistryUnavailableError as e:
            raise ManifestUnavailableError(f"registry unavailable ({e})") from None
        manifest = AppManifest.model_validate(app)
        if manifest.app_id != app_id:
            raise ResolutionError(f"registry returned app_id '{manifest.app_id}' for '{app_id}'.")
        return manifest

    def prompt(self, manifest: AppManifest, agent: AgentSpec) -> str:
        app_dir = self._apps_dir / manifest.app_id
        path = (app_dir / agent.prompt_ref).resolve()
        if not path.is_relative_to(app_dir) or not path.is_file():
            raise ResolutionError(
                f"Agent '{agent.agent_id}' of app '{manifest.app_id}': prompt_ref "
                f"'{agent.prompt_ref}' is not a file in the app's folder."
            )
        return path.read_text()
