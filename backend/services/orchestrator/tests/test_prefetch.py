"""`prefetch_tools`: tools the platform calls before the agent's loop (ADR-0028).

Called with no arguments, through the run's MCP session with the run
context, before the model is asked anything; each result is a data block
in the agent's first message, a call in its trace, and a failure like any
other tool's.
"""

import pytest

from app.tools.gateway import ToolResult
from proto_gen import agent_pb2
from run_context import RunContext
from tests.conftest import (
    APP_ID,
    RUNBOOK_RESULT,
    RUNBOOK_TOOL,
    FakeChatModel,
    FakeGateway,
    FakeRegistry,
    explanation,
    final,
    make_request,
    make_runner,
    tool_call,
)

pytestmark = pytest.mark.anyio

PAST = "similar-past-case-lookup"
PAST_TOOL = {**RUNBOOK_TOOL, "tool_id": PAST, "scope": "global", "description": "Past cases."}
PAST_RESULT = {
    "found": True,
    "alert_key": "disk_full:web-01",
    "cases": [{"case_id": 5, "verdict": "CONFIRMED_INCIDENT", "resolution_notes": "Rotated logs on /var."}],
}


def registry_with(prefetch: list[str], offered=(RUNBOOK_TOOL, PAST_TOOL), agent: int = 0) -> FakeRegistry:
    registry = FakeRegistry()
    a = registry.apps[APP_ID]["agents"][agent]
    a["tool_allowlist"] = [t["tool_id"] for t in (RUNBOOK_TOOL, PAST_TOOL)]
    a["tools"] = list(offered)
    a["prefetch_tools"] = prefetch
    return registry


def gateway(past: ToolResult = ToolResult(False, PAST_RESULT)) -> FakeGateway:
    return FakeGateway(results={"lookup_runbook": ToolResult(False, RUNBOOK_RESULT), PAST: past})


def first_message(llm: FakeChatModel, call: int = 0) -> str:
    return llm.calls[call]["messages"][0].content


async def test_prefetched_tool_runs_before_the_model_and_its_result_is_in_the_first_message(apps_dir):
    gw = gateway()
    llm = FakeChatModel(responses=[final("ESCALATE", "case 5: rotated logs last time")])

    response = await make_runner(apps_dir, llm, gw, registry=registry_with([PAST])).run(make_request())

    [(name, arguments, ctx)] = gw.calls
    assert (name, arguments) == (PAST, {})
    assert ctx == RunContext(app_id=APP_ID, agent_id="triage-agent", alert_id="alert-1", alert_key="disk_full:web-01")
    message = first_message(llm)
    assert "Looked up before you started:" in message
    assert f"kind=tool_result:{PAST}>>>" in message
    assert "Rotated logs on /var." in message
    # The model was asked once, with no tool call of its own: the lookup didn't depend on it.
    assert len(llm.calls) == 1
    assert [(t.tool_name, t.agent_id) for t in response.tool_calls] == [(PAST, "triage-agent")]


async def test_prefetched_tool_is_still_offered_and_the_prompt_says_not_to_call_it_again(apps_dir):
    llm = FakeChatModel(responses=[final()])
    await make_runner(apps_dir, llm, gateway(), registry=registry_with([PAST])).run(make_request())

    assert PAST in [t.name for t in llm.calls[0]["tools"]]
    assert "don't call those\ntools again" in llm.calls[0]["system"]


async def test_no_prefetch_tools_means_no_section_and_no_call(apps_dir):
    gw = gateway()
    llm = FakeChatModel(responses=[tool_call(), final()])
    await make_runner(apps_dir, llm, gw, registry=registry_with([])).run(make_request())

    assert "Looked up before you started" not in first_message(llm)
    assert [name for name, _, _ in gw.calls] == ["lookup_runbook"]


async def test_a_prefetch_tool_the_agent_isnt_offered_right_now_is_skipped(apps_dir):
    """Disabled in registry: dropped from `tools`, still in `prefetch_tools`."""
    gw = gateway()
    llm = FakeChatModel(responses=[final()])
    await make_runner(apps_dir, llm, gw, registry=registry_with([PAST], offered=(RUNBOOK_TOOL,))).run(make_request())

    assert gw.calls == []
    assert "Looked up before you started" not in first_message(llm)


async def test_a_failed_prefetch_escalates_like_any_failed_lookup(apps_dir):
    gw = gateway(past=ToolResult(True, {"error": "tool_failed"}))
    llm = FakeChatModel(responses=[final("SUPPRESS", "looks benign")])

    response = await make_runner(apps_dir, llm, gw, registry=registry_with([PAST])).run(make_request())

    assert response.decision == agent_pb2.ESCALATE
    assert f"tool '{PAST}' failed (tool_failed)" in " ".join(response.reasons)
    assert "kind=tool_result:" + PAST in first_message(llm)  # the agent saw the error result too


async def test_a_callable_prefetches_its_own_tools(apps_dir):
    (apps_dir / APP_ID / "prompts" / "summarizer.md").write_text("Summarize.")
    registry = registry_with([PAST], agent=1)
    registry.apps[APP_ID]["agents"][1]["invoke_on"] = ["ESCALATE"]
    gw = gateway()
    llm = FakeChatModel(responses=[final("ESCALATE", "needs a human"), explanation("case 5 again")])

    response = await make_runner(apps_dir, llm, gw, registry=registry).run(make_request())

    assert [(name, ctx.agent_id) for name, _, ctx in gw.calls] == [(PAST, "summarizer")]
    callable_call = next(c for c in llm.calls if "Explain this alert." in c["messages"][0].content)
    assert "Rotated logs on /var." in callable_call["messages"][0].content
    assert "summarizer: case 5 again" in response.reasons
