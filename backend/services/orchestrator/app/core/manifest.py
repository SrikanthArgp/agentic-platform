"""App Manifest resolution: `app_id` -> manifest, agent, prompt.

Day 3 interim: reads `backend/apps/{app_id}/manifest.yaml` from disk. Day 5
replaces `FileManifestStore` with a `registry` client (REST, 30s TTL cache,
docs/ARCHITECTURE.md §3); prompts stay files in this image either way
(`prompt_ref` is resolved here, §12).
"""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

# app_id arrives in Kafka messages; it becomes a path segment, so only slugs.
_APP_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class ResolutionError(LookupError):
    """The run names an app or agent that doesn't exist."""


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: str
    version: str
    role: Literal["entry", "callable"]
    prompt_ref: str
    tool_allowlist: list[str] = []
    invoke_on: list[str] = []


class ToolRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool_id: str
    version: str
    scope: Literal["app", "global"]


class AppManifest(BaseModel):
    # extra="ignore": fields added on later days (escalate_when, ...) must not
    # break an older orchestrator.
    model_config = ConfigDict(extra="ignore")

    app_id: str
    display_name: str
    agents: list[AgentSpec]
    tools: list[ToolRef] = []

    def agent(self, agent_id: str) -> AgentSpec:
        """The named agent, or the app's entry agent when `agent_id` is empty."""
        for agent in self.agents:
            if (agent.agent_id == agent_id) if agent_id else (agent.role == "entry"):
                return agent
        wanted = repr(agent_id) if agent_id else "an entry agent"
        raise ResolutionError(f"App '{self.app_id}' has no agent {wanted}.")


class FileManifestStore:
    def __init__(self, apps_dir: Path):
        self._apps_dir = apps_dir.resolve()

    def get(self, app_id: str) -> AppManifest:
        if not _APP_ID_RE.match(app_id):
            raise ResolutionError(f"Invalid app_id {app_id!r}.")
        path = self._apps_dir / app_id / "manifest.yaml"
        if not path.is_file():
            raise ResolutionError(f"No manifest for app_id '{app_id}'.")
        manifest = AppManifest.model_validate(yaml.safe_load(path.read_text()))
        if manifest.app_id != app_id:
            raise ResolutionError(f"{path} declares app_id '{manifest.app_id}', expected '{app_id}'.")
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
