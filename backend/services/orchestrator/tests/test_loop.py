"""The agent loop against a scripted LLM and a fake tool-gateway."""

import pytest

from app.agent.llm import LLMResponse, ToolCallRequest, ToolResultMessage
from app.agent.loop import not_evaluated_response
from app.core.manifest import ResolutionError
from app.tools.gateway import ToolResult
from proto_gen import agent_pb2
from run_context import RunContext
from tests.conftest import APP_ID, FakeGateway, FakeLLM, final, make_request, make_runner, tool_call

pytestmark = pytest.mark.anyio


async def test_tool_call_then_decision(apps_dir):
    llm = FakeLLM([LLMResponse(None, (tool_call(),)), final("AUTO_RESOLVE", "RB-001: transient spike")])
    gateway = FakeGateway()

    response = await make_runner(apps_dir, llm, gateway).run(make_request())

    assert response.app_id == APP_ID
    assert response.agent_id == "triage-agent"
    assert response.alert_id == "alert-1"
    assert response.decision == agent_pb2.AUTO_RESOLVE
    assert list(response.reasons) == ["RB-001: transient spike"]
    assert [(c.tool_name, c.result_summary[:13]) for c in response.tool_calls] == [
        ("lookup_runbook", '{"alert_type"')
    ]
    assert [name for name, _, _ in gateway.calls] == ["lookup_runbook"]


async def test_tool_calls_carry_app_id_in_context_never_in_arguments(apps_dir):
    llm = FakeLLM([LLMResponse(None, (tool_call(alert_type="disk_full"),)), final()])
    gateway = FakeGateway()

    await make_runner(apps_dir, llm, gateway).run(make_request())

    [(_, arguments, context)] = gateway.calls
    assert context == RunContext(app_id=APP_ID, agent_id="triage-agent", alert_id="alert-1")
    assert arguments == {"alert_type": "disk_full"}


async def test_only_allowlisted_tools_of_this_app_are_offered(apps_dir):
    llm = FakeLLM([final()])
    await make_runner(apps_dir, llm).run(make_request())

    assert [t.name for t in llm.calls[0]["tools"]] == ["lookup_runbook"]


async def test_tool_not_on_allowlist_is_refused_without_calling_gateway(apps_dir):
    llm = FakeLLM([LLMResponse(None, (tool_call("other_app_tool"),)), final("ESCALATE", "no runbook")])
    gateway = FakeGateway()

    response = await make_runner(apps_dir, llm, gateway).run(make_request())

    assert gateway.calls == []
    assert response.tool_calls[0].result_summary == "error: tool_not_allowed"
    # A refused call is the agent's mistake, not an infra failure: no forced escalation reason.
    assert list(response.reasons) == ["no runbook"]


async def test_tool_results_go_back_to_the_llm_as_data_blocks(apps_dir):
    llm = FakeLLM([LLMResponse(None, (tool_call(),)), final()])
    await make_runner(apps_dir, llm).run(make_request())

    tool_message = llm.calls[1]["messages"][-1]
    assert isinstance(tool_message, ToolResultMessage)
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.content.startswith("<<<DATA ")
    assert "kind=tool_result:lookup_runbook>>>" in tool_message.content
    assert "RB-001" in tool_message.content


async def test_infra_tool_failure_forces_escalate(apps_dir):
    gateway = FakeGateway(results={"lookup_runbook": ToolResult(True, {"error": "tool_failed"})})
    llm = FakeLLM([LLMResponse(None, (tool_call(),)), final("SUPPRESS", "looks benign")])

    response = await make_runner(apps_dir, llm, gateway).run(make_request())

    assert response.decision == agent_pb2.ESCALATE
    assert response.reasons[0] == "looks benign"
    assert "tool 'lookup_runbook' failed (tool_failed)" in response.reasons[1]
    assert "escalated because a tool lookup failed" in response.reasons[2]


async def test_invalid_arguments_do_not_force_escalate(apps_dir):
    gateway = FakeGateway(results={"lookup_runbook": ToolResult(True, {"error": "invalid_arguments"})})
    llm = FakeLLM([LLMResponse(None, (tool_call(),)), final("AUTO_RESOLVE", "retried fine")])

    response = await make_runner(apps_dir, llm, gateway).run(make_request())

    assert response.decision == agent_pb2.AUTO_RESOLVE


async def test_non_object_arguments_never_reach_gateway(apps_dir):
    bad = ToolCallRequest(id="c", name="lookup_runbook", arguments=None, raw_arguments="[1]")
    gateway = FakeGateway()
    llm = FakeLLM([LLMResponse(None, (bad,)), final()])

    response = await make_runner(apps_dir, llm, gateway).run(make_request())

    assert gateway.calls == []
    assert response.tool_calls[0].result_summary == "error: invalid_arguments"


async def test_unparseable_final_answer_escalates(apps_dir):
    llm = FakeLLM([LLMResponse("Probably fine, suppress it.")])
    response = await make_runner(apps_dir, llm).run(make_request())

    assert response.decision == agent_pb2.ESCALATE
    assert "not JSON" in response.reasons[0]


async def test_endless_tool_calls_stop_and_escalate(apps_dir):
    llm = FakeLLM([LLMResponse(None, (tool_call(call_id=f"c{i}"),)) for i in range(3)])
    gateway = FakeGateway()

    response = await make_runner(apps_dir, llm, gateway, max_tool_rounds=2).run(make_request())

    assert response.decision == agent_pb2.ESCALATE
    assert "within 2 tool-call rounds" in response.reasons[0]
    assert len(gateway.calls) == 2


async def test_empty_agent_id_runs_the_entry_agent_and_named_agent_runs_that_one(apps_dir):
    llm = FakeLLM([final()])
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
        await make_runner(apps_dir, FakeLLM([])).run(request)


def test_not_evaluated_response_escalates_with_reason():
    response = not_evaluated_response(make_request(), "LLM unavailable")
    assert response.decision == agent_pb2.ESCALATE
    assert response.alert_id == "alert-1"
    assert "not evaluated: LLM unavailable" in response.reasons[0]
