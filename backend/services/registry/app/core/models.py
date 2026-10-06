"""What `registry` accepts and serves: tool registrations and App Manifests.

Input models are strict (`extra="forbid"`): a typo'd manifest field is a
`422` at registration, not a field silently ignored at runtime. Readers
(`orchestrator`, `ingestion`, `tool-gateway`) parse the served shape
leniently, so a field added here later doesn't break an older image.

The manifest shape is docs/ARCHITECTURE.md §3. `registry` checks shapes
here and cross-references (tools exist, one entry agent, ...) in
`validation.py`.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SLUG = r"^[a-z][a-z0-9-]{0,62}$"
TOOL_ID = r"^[a-z][a-z0-9_-]{0,63}$"
VERSION = r"^\d+\.\d+\.\d+$"

Scope = Literal["app", "global"]
Scalar = str | int | float | bool


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ tools


class ToolIn(_Strict):
    """Body of `PUT /tools/{tool_id}/versions/{version}`."""

    description: str = Field(min_length=1)
    scope: Scope
    # The owning app for scope="app"; must be absent for global tools.
    app_id: str | None = Field(default=None, pattern=SLUG)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    # Must be true (ADR-0004, docs/ARCHITECTURE.md §13 T8). A bool rather
    # than Literal[True] so a false value gets its own clear error.
    read_only: bool


class Tool(BaseModel):
    tool_id: str
    version: str
    description: str
    scope: Scope
    app_id: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    read_only: bool
    # Runtime switch: a disabled tool stays registered (manifests can still
    # reference it) but no agent is offered it or may call it.
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def definition(self) -> dict[str, Any]:
        """The fields `PUT` compares to decide whether anything changed."""
        return self.model_dump(include={"description", "scope", "app_id", "input_schema", "output_schema", "read_only"})


class ToolEnabledIn(_Strict):
    enabled: bool


# --------------------------------------------------------------- manifest


class AgentIn(_Strict):
    agent_id: str = Field(pattern=SLUG)
    version: str = Field(pattern=VERSION)
    role: Literal["entry", "callable"]
    prompt_ref: str = Field(min_length=1)
    tool_allowlist: list[str] = []
    invoke_on: list[str] = []


class ToolRefIn(_Strict):
    tool_id: str = Field(pattern=TOOL_ID)
    version: str = Field(pattern=VERSION)
    scope: Scope


class EscalateRuleIn(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # "alert.<envelope field>", "payload.<path>" or "context.<GetContextResponse
    # field path>" (ADR-0010).
    field: str
    in_: list[Scalar] = Field(alias="in", min_length=1)


class SupervisorIn(_Strict):
    # Below this, orchestrator's supervisor escalates; omitted -> its
    # MIN_CONFIDENCE default (docs/adr/0020).
    min_confidence: float | None = Field(default=None, ge=0, le=1)


class ManifestIn(_Strict):
    """Body of `PUT /apps/{app_id}`: `backend/apps/{app_id}/manifest.yaml` as JSON."""

    app_id: str = Field(pattern=SLUG)
    display_name: str = Field(min_length=1)
    agents: list[AgentIn]
    tools: list[ToolRefIn] = []
    event_schema_ref: str = Field(min_length=1)
    alert_key_fields: list[str]
    memory_namespace: str = Field(pattern=SLUG)
    escalate_when: list[EscalateRuleIn] = []
    supervisor: SupervisorIn | None = None

    def stored(self) -> dict[str, Any]:
        """The JSON document kept in the `apps` row."""
        return self.model_dump(mode="json", by_alias=True)


# --------------------------------------------------------- served shapes


class EffectiveTool(BaseModel):
    """A tool an agent may call right now: allowlisted, declared, enabled."""

    tool_id: str
    version: str
    scope: Scope
    description: str
    input_schema: dict[str, Any]


class ResolvedAgent(BaseModel):
    agent_id: str
    version: str
    role: Literal["entry", "callable"]
    prompt_ref: str
    tool_allowlist: list[str]
    invoke_on: list[str]
    # Computed here, once, so orchestrator (tool definitions) and
    # tool-gateway (per-call re-check) can't disagree about it.
    tools: list[EffectiveTool]


class ResolvedToolRef(BaseModel):
    tool_id: str
    version: str
    scope: Scope
    enabled: bool


class ResolvedApp(BaseModel):
    """`GET /apps/{app_id}`: the manifest, with each agent's effective tools."""

    app_id: str
    display_name: str
    agents: list[ResolvedAgent]
    tools: list[ResolvedToolRef]
    event_schema_ref: str
    alert_key_fields: list[str]
    memory_namespace: str
    escalate_when: list[dict[str, Any]]
    supervisor: dict[str, Any] | None = None
    updated_at: datetime | None = None


class AppRecord(BaseModel):
    app_id: str
    display_name: str
    manifest: dict[str, Any]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UpsertResult(BaseModel):
    created: bool
    changed: bool
