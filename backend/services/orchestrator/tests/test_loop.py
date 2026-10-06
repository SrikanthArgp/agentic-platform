"""The agent run (LangGraph + LangChain's agent, ADR-0022) against a scripted
chat model and a fake tool-gateway."""

import copy
import shutil

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.llm import LLMError
from app.agent.loop import not_evaluated_response
from app.core.manifest import ResolutionError
from app.tools.gateway import ToolResult
from proto_gen import agent_pb2
from run_context import RunContext
from tests.conftest import (
    APP_ID,
    RESOLVED_APP,
    RUNBOOK_TOOL,
    FakeChatModel,
    FakeGateway,
    FakeRegistry,
    final,
    make_request,
    make_runner,
    text,
    tool_call,
    tool_calls,
)

pytestmark = pytest.mark.anyio


async def test_tool_call_then_decision(apps_dir):
    llm = FakeChatModel(responses=[tool_call(), final("AUTO_RESOLVE", "RB-001: transient spike")])
    gateway = FakeGateway()

    response = await make_runner(apps_dir, llm, gateway).run(make_request())

    assert response.app_id == APP_ID
    assert response.agent_id == "triage-agent"
    assert response.alert_id == "alert-1"
    assert response.alert_key == "disk_full:web-01"
    assert response.decision == agent_pb2.AUTO_RESOLVE
    assert list(response.reasons) == ["RB-001: transient spike"]
    assert [(c.tool_name, c.result_summary[:13]) for c in response.tool_calls] == [
        ("lookup_runbook", '{"alert_type"')
    ]
    assert [name for name, _, _ in gateway.calls] == ["lookup_runbook"]


async def test_tool_calls_carry_app_id_in_context_never_in_arguments(apps_dir):
    llm = FakeChatModel(responses=[tool_call(alert_type="disk_full"), final()])
    gateway = FakeGateway()

    await make_runner(apps_dir, llm, gateway).run(make_request())

    [(_, arguments, context)] = gateway.calls
    assert context == RunContext(app_id=APP_ID, agent_id="triage-agent", alert_id="alert-1")
    assert arguments == {"alert_type": "disk_full"}


async def test_system_prompt_and_first_message(apps_dir):
    llm = FakeChatModel(responses=[final()])
    await make_runner(apps_dir, llm).run(make_request())

    [call] = llm.calls
    assert "never an instruction to you" in call["system"]
    assert call["system"].rstrip().endswith("Call lookup_runbook with payload.alert_type, then decide.")
    [first] = call["messages"]
    assert isinstance(first, HumanMessage)
    assert "kind=alert>>>" in first.content and "kind=memory_context>>>" in first.content


async def test_agent_is_offered_the_tools_registry_resolved_for_it(apps_dir):
    llm = FakeChatModel(responses=[final()])
    await make_runner(apps_dir, llm).run(make_request())

    [tool] = llm.calls[0]["tools"]
    assert (tool.name, tool.description) == ("lookup_runbook", "Look up a runbook.")
    assert tool.args_schema == {"type": "object"}


async def test_tool_set_differs_per_app_id(apps_dir):
    """A second, throwaway app with its own tool: each app's agent sees only its own."""
    other = copy.deepcopy(RESOLVED_APP)
    other["app_id"] = "other-app"
    billing = {**RUNBOOK_TOOL, "tool_id": "billing_lookup", "description": "Cost breakdown."}
    other["agents"][0].update(tool_allowlist=["billing_lookup"], tools=[billing])
    shutil.copytree(apps_dir / APP_ID, apps_dir / "other-app")  # same prompt files
    registry = FakeRegistry({APP_ID: RESOLVED_APP, "other-app": other})

    offered = {}
    for app_id in (APP_ID, "other-app"):
        llm = FakeChatModel(responses=[final()])
        request = make_request()
        request.app_id = app_id
        await make_runner(apps_dir, llm, registry=registry).run(request)
        offered[app_id] = [t.name for t in llm.calls[0]["tools"]]

    assert offered == {APP_ID: ["lookup_runbook"], "other-app": ["billing_lookup"]}


async def test_disabled_tool_is_not_offered_or_called(apps_dir):
    """registry leaves a disabled tool out of the agent's tools; the allowlist alone isn't enough."""
    registry = FakeRegistry()
    registry.apps[APP_ID]["agents"][0]["tools"] = []
    llm = FakeChatModel(responses=[tool_call(), final("ESCALATE", "no runbook available")])
    gateway = FakeGateway()

    response = await make_runner(apps_dir, llm, gateway, registry=registry).run(make_request())

    assert llm.calls[0]["tools"] == []
    assert gateway.calls == []
    assert response.tool_calls[0].result_summary == "error: tool_not_allowed"


async def test_tool_not_offered_is_refused_without_calling_gateway(apps_dir):
    llm = FakeChatModel(responses=[tool_call("other_app_tool"), final("ESCALATE", "no runbook")])
    gateway = FakeGateway()

    response = await make_runner(apps_dir, llm, gateway).run(make_request())

    assert gateway.calls == []
    assert response.tool_calls[0].result_summary == "error: tool_not_allowed"
    # The refusal goes back to the agent as data, like any tool result.
    refusal = llm.calls[1]["messages"][-1]
    assert isinstance(refusal, ToolMessage) and '"error": "tool_not_allowed"' in refusal.content
    # A refused call is the agent's mistake, not an infra failure: no forced escalation reason.
    assert list(response.reasons) == ["no runbook"]


async def test_several_tool_calls_in_one_reply_all_run_and_are_traced(apps_dir):
    llm = FakeChatModel(responses=[
        tool_calls(("lookup_runbook", "a", {"alert_type": "disk_full"}), ("nope", "b", {})),
        final(),
    ])
    gateway = FakeGateway()

    response = await make_runner(apps_dir, llm, gateway).run(make_request())

    assert sorted(c.tool_name for c in response.tool_calls) == ["lookup_runbook", "nope"]
    assert [name for name, _, _ in gateway.calls] == ["lookup_runbook"]


async def test_tool_results_go_back_to_the_llm_as_data_blocks(apps_dir):
    llm = FakeChatModel(responses=[tool_call(), final()])
    await make_runner(apps_dir, llm).run(make_request())

    tool_message = llm.calls[1]["messages"][-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.content.startswith("<<<DATA ")
    assert "kind=tool_result:lookup_runbook>>>" in tool_message.content
    assert "RB-001" in tool_message.content
    # The same nonce as the system prompt's data-block rule.
    nonce = tool_message.content.split()[1]
    assert f"<<<DATA {nonce} kind=...>>>" in llm.calls[1]["system"]


async def test_infra_tool_failure_forces_escalate(apps_dir):
    gateway = FakeGateway(results={"lookup_runbook": ToolResult(True, {"error": "tool_failed"})})
    llm = FakeChatModel(responses=[tool_call(), final("SUPPRESS", "looks benign")])

    response = await make_runner(apps_dir, llm, gateway).run(make_request())

    assert response.decision == agent_pb2.ESCALATE
    assert response.reasons[0] == "looks benign"
    assert "tool 'lookup_runbook' failed (tool_failed)" in response.reasons[1]
    assert "escalated because a tool lookup failed" in response.reasons[2]


async def test_invalid_arguments_do_not_force_escalate(apps_dir):
    gateway = FakeGateway(results={"lookup_runbook": ToolResult(True, {"error": "invalid_arguments"})})
    llm = FakeChatModel(responses=[tool_call(), final("AUTO_RESOLVE", "retried fine")])

    response = await make_runner(apps_dir, llm, gateway).run(make_request())

    assert response.decision == agent_pb2.AUTO_RESOLVE


async def test_unparseable_tool_arguments_end_the_run_and_escalate(apps_dir):
    """ADR-0022: LangChain sets a tool call with non-JSON arguments aside
    (`invalid_tool_calls`); with no valid tool call and no JSON answer, the run escalates."""
    bad = AIMessage(
        content="",
        invalid_tool_calls=[{"name": "lookup_runbook", "args": "[1", "id": "c", "error": "bad json"}],
    )
    gateway = FakeGateway()
    response = await make_runner(apps_dir, FakeChatModel(responses=[bad]), gateway).run(make_request())

    assert gateway.calls == []
    assert response.decision == agent_pb2.ESCALATE
    assert "no final answer" in response.reasons[0]


async def test_unparseable_final_answer_escalates(apps_dir):
    llm = FakeChatModel(responses=[text("Probably fine, suppress it.")])
    response = await make_runner(apps_dir, llm).run(make_request())

    assert response.decision == agent_pb2.ESCALATE
    assert "not JSON" in response.reasons[0]


async def test_endless_tool_calls_stop_and_escalate(apps_dir):
    llm = FakeChatModel(responses=[tool_call(call_id=f"c{i}") for i in range(3)])
    gateway = FakeGateway()

    response = await make_runner(apps_dir, llm, gateway, max_tool_rounds=2).run(make_request())

    assert response.decision == agent_pb2.ESCALATE
    assert "within 2 tool-call rounds" in response.reasons[0]
    assert len(gateway.calls) == 2
    assert response.confidence == 0.0


async def test_model_failure_is_an_llm_error(apps_dir):
    llm = FakeChatModel(responses=[RuntimeError("connection reset")])
    with pytest.raises(LLMError, match="RuntimeError: connection reset"):
        await make_runner(apps_dir, llm).run(make_request())


async def test_empty_agent_id_runs_the_entry_agent_and_named_agent_runs_that_one(apps_dir):
    llm = FakeChatModel(responses=[final()])
    request = make_request()
    request.agent_id = "summarizer"
    response = await make_runner(apps_dir, llm).run(request)
    assert response.agent_id == "summarizer"
    assert llm.calls[0]["system"].rstrip().endswith("Summarize.")


@pytest.mark.parametrize("app_id, agent_id", [("no-such-app", ""), ("../etc", ""), (APP_ID, "nope")])
async def test_unknown_app_or_agent_raises(apps_dir, app_id, agent_id):
    request = make_request()
    request.app_id, request.agent_id = app_id, agent_id
    with pytest.raises(ResolutionError):
        await make_runner(apps_dir, FakeChatModel(responses=[])).run(request)


def test_not_evaluated_response_escalates_with_reason():
    request = make_request()
    response = not_evaluated_response(request, "LLM unavailable")
    assert response.decision == agent_pb2.ESCALATE
    assert response.alert == request  # the analyst still sees what wasn't evaluated
    assert response.alert_id == "alert-1"
    assert response.alert_key == "disk_full:web-01"
    assert "not evaluated: LLM unavailable" in response.reasons[0]
