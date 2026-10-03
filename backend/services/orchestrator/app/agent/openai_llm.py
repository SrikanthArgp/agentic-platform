"""OpenAI adapter for the `LLMClient` interface (Chat Completions + function tools).

The only module that imports the `openai` SDK (ADR-0015). The SDK reads
`OPENAI_API_KEY` from the environment itself.
"""

import json
from collections.abc import Sequence
from typing import Any

import openai

from app.agent.llm import (
    AssistantMessage,
    LLMError,
    LLMResponse,
    Message,
    ToolCallRequest,
    ToolDefinition,
    ToolResultMessage,
    Usage,
    UserMessage,
)


class OpenAIChatClient:
    def __init__(self, model: str, client: Any | None = None, timeout_s: float = 60.0):
        self.model = model
        self._client = client or openai.AsyncOpenAI(timeout=timeout_s, max_retries=2)

    async def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *map(to_openai_message, messages)],
        }
        if tools:
            request["tools"] = [to_openai_tool(t) for t in tools]
        try:
            response = await self._client.chat.completions.create(**request)
        except openai.OpenAIError as e:
            raise LLMError(f"OpenAI request failed: {type(e).__name__}") from e
        return from_openai_response(response)


def to_openai_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": tool.input_schema},
    }


def to_openai_message(message: Message) -> dict[str, Any]:
    match message:
        case UserMessage(content=content):
            return {"role": "user", "content": content}
        case AssistantMessage(content=content, tool_calls=tool_calls):
            out: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                out["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.raw_arguments or json.dumps(c.arguments or {})},
                    }
                    for c in tool_calls
                ]
            return out
        case ToolResultMessage(tool_call_id=tool_call_id, content=content):
            return {"role": "tool", "tool_call_id": tool_call_id, "content": content}
    raise TypeError(f"Unknown message type {type(message).__name__}")


def from_openai_response(response: Any) -> LLMResponse:
    if not response.choices:
        raise LLMError("OpenAI response has no choices.")
    message = response.choices[0].message
    calls = []
    for call in message.tool_calls or ():
        if call.type != "function":
            raise LLMError(f"Unsupported tool call type '{call.type}'.")
        calls.append(
            ToolCallRequest(
                id=call.id,
                name=call.function.name,
                arguments=_json_object(call.function.arguments),
                raw_arguments=call.function.arguments,
            )
        )
    usage = response.usage
    return LLMResponse(
        text=message.content,
        tool_calls=tuple(calls),
        usage=Usage(usage.prompt_tokens, usage.completion_tokens) if usage else Usage(),
    )


def _json_object(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
