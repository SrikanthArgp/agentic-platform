"""The provider-agnostic LLM interface the agent loop talks to.

The loop never imports a provider SDK: it builds these messages and tool
definitions and gets back an `LLMResponse`. One adapter per provider
(`openai_llm.py` today, ADR-0015) translates. This is also the seam for a
per-agent `model_ref` later (docs/ENTERPRISE_READINESS.md §3).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    """The provider call failed (network, auth, rate limit, bad response)."""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    # None when the model produced arguments that aren't a JSON object.
    arguments: dict[str, Any] | None
    raw_arguments: str = ""


@dataclass(frozen=True)
class UserMessage:
    content: str


@dataclass(frozen=True)
class AssistantMessage:
    content: str | None
    tool_calls: tuple[ToolCallRequest, ...] = ()


@dataclass(frozen=True)
class ToolResultMessage:
    tool_call_id: str
    content: str


Message = UserMessage | AssistantMessage | ToolResultMessage


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str | None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    usage: Usage = field(default_factory=Usage)


class LLMClient(Protocol):
    model: str

    async def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> LLMResponse: ...
