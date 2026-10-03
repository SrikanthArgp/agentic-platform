"""The OpenAI adapter's translation, against a stub SDK client (no network)."""

from types import SimpleNamespace

import openai
import pytest

from app.agent.llm import (
    AssistantMessage,
    LLMError,
    ToolCallRequest,
    ToolDefinition,
    ToolResultMessage,
    UserMessage,
)
from app.agent.openai_llm import OpenAIChatClient

pytestmark = pytest.mark.anyio


def _response(content=None, tool_calls=None, usage=(12, 3)):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
    )


def _tool_call(arguments: str, type_: str = "function"):
    return SimpleNamespace(id="call-1", type=type_, function=SimpleNamespace(name="lookup_runbook", arguments=arguments))


class StubSDK:
    def __init__(self, response=None, error: Exception | None = None):
        self.requests: list[dict] = []
        self._response, self._error = response, error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **request):
        self.requests.append(request)
        if self._error:
            raise self._error
        return self._response


async def test_request_shape_system_messages_and_tools():
    sdk = StubSDK(_response(content='{"decision": "ESCALATE", "reasons": ["x"]}'))
    call = ToolCallRequest(id="call-1", name="lookup_runbook", arguments={"alert_type": "a"}, raw_arguments='{"alert_type":"a"}')
    messages = [UserMessage("alert"), AssistantMessage(None, (call,)), ToolResultMessage("call-1", "result")]
    tools = [ToolDefinition("lookup_runbook", "Look up.", {"type": "object"})]

    await OpenAIChatClient("gpt-test", client=sdk).complete(system="SYS", messages=messages, tools=tools)

    [request] = sdk.requests
    assert request["model"] == "gpt-test"
    assert request["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "alert"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "lookup_runbook", "arguments": '{"alert_type":"a"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]
    assert request["tools"] == [
        {"type": "function", "function": {"name": "lookup_runbook", "description": "Look up.", "parameters": {"type": "object"}}}
    ]


async def test_no_tools_key_when_no_tools():
    sdk = StubSDK(_response(content="{}"))
    await OpenAIChatClient("m", client=sdk).complete(system="S", messages=[UserMessage("u")], tools=[])
    assert "tools" not in sdk.requests[0]


async def test_response_text_tool_calls_and_usage():
    sdk = StubSDK(_response(tool_calls=[_tool_call('{"alert_type": "disk_full"}')]))
    response = await OpenAIChatClient("m", client=sdk).complete(system="S", messages=[], tools=[])

    [call] = response.tool_calls
    assert (call.id, call.name, call.arguments) == ("call-1", "lookup_runbook", {"alert_type": "disk_full"})
    assert (response.usage.input_tokens, response.usage.output_tokens) == (12, 3)


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"str"'])
async def test_malformed_arguments_become_none(raw):
    sdk = StubSDK(_response(tool_calls=[_tool_call(raw)]))
    response = await OpenAIChatClient("m", client=sdk).complete(system="S", messages=[], tools=[])
    assert response.tool_calls[0].arguments is None
    assert response.tool_calls[0].raw_arguments == raw


async def test_sdk_errors_become_llm_error():
    sdk = StubSDK(error=openai.APIConnectionError(request=SimpleNamespace()))
    with pytest.raises(LLMError):
        await OpenAIChatClient("m", client=sdk).complete(system="S", messages=[], tools=[])


async def test_non_function_tool_call_is_an_llm_error():
    sdk = StubSDK(_response(tool_calls=[_tool_call("{}", type_="custom")]))
    with pytest.raises(LLMError):
        await OpenAIChatClient("m", client=sdk).complete(system="S", messages=[], tools=[])
