"""Callable agents and `invoke_on` delegation (plan.md Day 8, ADR-0012).

After the entry decision is final (supervisor and guardrails included),
every callable whose `invoke_on` lists it runs in parallel; their reasons
are folded into the one response, prefixed and in manifest order.
"""

import time

import pytest

from app.agent.graph import route_callables
from app.core.manifest import AppManifest
from proto_gen import agent_pb2
from run_context import RunContext
from tests.conftest import (
    APP_ID,
    RESOLVED_APP,
    RUNBOOK_TOOL,
    FakeChatModel,
    FakeGateway,
    FakeMemory,
    FakeRegistry,
    context,
    explanation,
    final,
    make_request,
    make_runner,
    text,
    tool_call,
)

pytestmark = pytest.mark.anyio

# Each callable's prompt text is also how FakeChatModel tells them apart.
SUMMARIZER = "Write the root cause."
BLAST = "Assess the blast radius."
CHANGES_TOOL = {**RUNBOOK_TOOL, "tool_id": "recent-changes-lookup", "description": "Recent changes."}


def callable_agent(agent_id: str, invoke_on: list[str], tools=()) -> dict:
    return {
        "agent_id": agent_id, "version": "0.1.0", "role": "callable",
        "prompt_ref": f"prompts/{agent_id}.md", "tool_allowlist": [t["tool_id"] for t in tools],
        "invoke_on": invoke_on, "tools": list(tools),
    }


@pytest.fixture
def app_with_callables(apps_dir):
    (apps_dir / APP_ID / "prompts" / "root-cause-summarizer.md").write_text(SUMMARIZER)
    (apps_dir / APP_ID / "prompts" / "blast-radius.md").write_text(BLAST)

    def make(*agents: dict, escalate_when=()) -> FakeRegistry:
        registry = FakeRegistry()
        app = registry.apps[APP_ID]
        app["agents"] = [app["agents"][0], *agents]
        app["escalate_when"] = list(escalate_when)
        return registry

    return make


async def run(apps_dir, registry, llm, request=None, **kwargs):
    return await make_runner(apps_dir, llm, registry=registry, **kwargs).run(request or make_request())


# --- invoke_on selection ------------------------------------------------------

@pytest.mark.parametrize(
    "callables, decision, selected",
    [
        ([], "ESCALATE", []),
        ([("rcs", ["ESCALATE"])], "ESCALATE", ["rcs"]),
        ([("rcs", ["ESCALATE"])], "AUTO_RESOLVE", []),
        ([("rcs", ["ESCALATE"])], "SUPPRESS", []),
        ([("rcs", ["ESCALATE"]), ("blast", ["ESCALATE", "SUPPRESS"])], "ESCALATE", ["rcs", "blast"]),
        ([("rcs", ["ESCALATE"]), ("blast", ["ESCALATE", "SUPPRESS"])], "SUPPRESS", ["blast"]),
        ([("rcs", ["ESCALATE"]), ("blast", ["ESCALATE", "SUPPRESS"])], "AUTO_RESOLVE", []),
        # Manifest order, not invoke_on order or name order.
        ([("zeta", ["AUTO_RESOLVE"]), ("alpha", ["AUTO_RESOLVE"])], "AUTO_RESOLVE", ["zeta", "alpha"]),
    ],
)
def test_invoke_on_selection(callables, decision, selected):
    app = {**RESOLVED_APP, "agents": [RESOLVED_APP["agents"][0], *(callable_agent(a, on) for a, on in callables)]}
    manifest = AppManifest.model_validate(app)
    assert [a.agent_id for a in manifest.callables_for(decision)] == selected


def test_the_entry_agent_is_never_selected():
    manifest = AppManifest.model_validate(RESOLVED_APP)
    assert all(a.role == "callable" for d in ("AUTO_RESOLVE", "ESCALATE", "SUPPRESS") for a in manifest.callables_for(d))


def test_no_match_goes_straight_to_fold():
    manifest = AppManifest.model_validate(RESOLVED_APP)
    state = {"decision": agent_pb2.SUPPRESS, "manifest": manifest, "reasons": [], "platform_reasons": []}
    assert route_callables(state) == "fold"


# --- a whole run ---------------------------------------------------------------

async def test_escalate_runs_the_summarizer_and_folds_its_reasons(apps_dir, app_with_callables):
    registry = app_with_callables(callable_agent("root-cause-summarizer", ["ESCALATE"]))
    llm = FakeChatModel(
        responses=[final("ESCALATE", "disk above 95%")],
        scripts={SUMMARIZER: [explanation("probable cause: CHG-20931 raised log level", "no deploy in window")]},
    )
    response = await run(apps_dir, registry, llm)

    assert response.decision == agent_pb2.ESCALATE
    assert response.agent_id == "triage-agent"
    assert list(response.reasons) == [
        "disk above 95%",
        "root-cause-summarizer: probable cause: CHG-20931 raised log level",
        "root-cause-summarizer: no deploy in window",
    ]


@pytest.mark.parametrize("decision", ["AUTO_RESOLVE", "SUPPRESS"])
async def test_other_decisions_trigger_nothing(apps_dir, app_with_callables, decision):
    registry = app_with_callables(callable_agent("root-cause-summarizer", ["ESCALATE"]))
    llm = FakeChatModel(responses=[final(decision)], scripts={SUMMARIZER: []})
    response = await run(apps_dir, registry, llm)

    assert response.decision == agent_pb2.Decision.Value(decision)
    assert len(llm.calls) == 1
    assert list(response.reasons) == ["RB-001 says so"]


async def test_invoke_on_keys_off_the_final_decision_not_the_agents(apps_dir, app_with_callables):
    """The agent says AUTO_RESOLVE; a guardrail raises it to ESCALATE; the summarizer runs."""
    registry = app_with_callables(
        callable_agent("root-cause-summarizer", ["ESCALATE"]),
        escalate_when=[{"field": "alert.severity", "in": ["critical"]}],
    )
    llm = FakeChatModel(responses=[final("AUTO_RESOLVE")], scripts={SUMMARIZER: [explanation("why")]})
    request = make_request()
    request.severity = "critical"
    response = await run(apps_dir, registry, llm, request)

    assert response.decision == agent_pb2.ESCALATE
    assert response.reasons[-2].startswith("orchestrator: guardrail")
    assert response.reasons[-1] == "root-cause-summarizer: why"


async def test_supervisor_escalation_also_triggers_callables(apps_dir, app_with_callables):
    registry = app_with_callables(callable_agent("root-cause-summarizer", ["ESCALATE"]))
    llm = FakeChatModel(responses=[final("SUPPRESS", confidence=0.95)], scripts={SUMMARIZER: [explanation("why")]})
    response = await run(apps_dir, registry, llm, memory=FakeMemory(context(novel=True)))

    assert response.decision == agent_pb2.ESCALATE
    assert response.reasons[-1] == "root-cause-summarizer: why"


async def test_callables_run_concurrently_and_fold_in_manifest_order(apps_dir, app_with_callables):
    registry = app_with_callables(
        callable_agent("root-cause-summarizer", ["ESCALATE"]), callable_agent("blast-radius", ["ESCALATE"])
    )
    llm = FakeChatModel(
        responses=[final("ESCALATE", "x")],
        scripts={SUMMARIZER: [explanation("cause")], BLAST: [explanation("two hosts")]},
        # The first in manifest order finishes last.
        delays={SUMMARIZER: 0.4, BLAST: 0.3},
    )
    started = time.perf_counter()
    response = await run(apps_dir, registry, llm)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.6  # ~max(0.4, 0.3), not their sum (0.7)
    assert list(response.reasons) == ["x", "root-cause-summarizer: cause", "blast-radius: two hosts"]


async def test_callable_sees_the_alert_history_and_final_decision_as_data(apps_dir, app_with_callables):
    registry = app_with_callables(callable_agent("root-cause-summarizer", ["ESCALATE"]))
    llm = FakeChatModel(responses=[final("ESCALATE", "disk above 95%")], scripts={SUMMARIZER: [explanation("y")]})
    await run(apps_dir, registry, llm)

    call = next(c for c in llm.calls if SUMMARIZER in c["system"])
    assert "explanation agent" in call["system"]
    assert '{{"reasons"' not in call["system"] and '{"reasons": ["...", "..."]}' in call["system"]
    assert '"decision": "AUTO_RESOLVE" | "ESCALATE"' not in call["system"]
    [first] = call["messages"]
    for kind in ("alert", "memory_context", "triage_decision"):
        assert f"kind={kind}>>>" in first.content
    assert '"disk above 95%"' in first.content
    assert '"fired_at": "2023-11-14T22:13:20Z"' in first.content


async def test_callable_uses_its_own_tools_and_run_context(apps_dir, app_with_callables):
    registry = app_with_callables(callable_agent("root-cause-summarizer", ["ESCALATE"], tools=[CHANGES_TOOL]))
    gateway = FakeGateway()
    gateway.results["recent-changes-lookup"] = gateway.results["lookup_runbook"]
    llm = FakeChatModel(
        responses=[final("ESCALATE")],
        scripts={SUMMARIZER: [tool_call("recent-changes-lookup", service_or_host="web-01"), explanation("z")]},
    )
    response = await run(apps_dir, registry, llm, gateway=gateway)

    entry_call = llm.calls[0]
    callable_call = next(c for c in llm.calls if SUMMARIZER in c["system"])
    assert [t.name for t in entry_call["tools"]] == ["lookup_runbook"]  # callables are never the entry's tools
    assert [t.name for t in callable_call["tools"]] == ["recent-changes-lookup"]
    [(name, _, ctx)] = gateway.calls
    assert name == "recent-changes-lookup"
    assert ctx == RunContext(app_id=APP_ID, agent_id="root-cause-summarizer", alert_id="alert-1")
    # The response's tool trace is the entry agent's; the callable's is in its reasons/traces.
    assert list(response.tool_calls) == []


@pytest.mark.parametrize(
    "script, why",
    [
        ([RuntimeError("provider down")], "LLM unavailable: RuntimeError: provider down"),
        ([text("not json")], "answer didn't parse: Agent's final answer is not JSON."),
        ([final("ESCALATE")], None),  # a decision-shaped answer still has reasons: accepted
        ([text('{"reasons": []}')], "answer didn't parse: Agent's final answer is invalid (reasons)."),
        ([tool_call(call_id=f"c{i}") for i in range(3)], "no answer within 2 tool-call rounds"),
    ],
)
async def test_a_failing_callable_never_blocks_or_changes_the_decision(apps_dir, app_with_callables, script, why):
    registry = app_with_callables(
        callable_agent("root-cause-summarizer", ["ESCALATE"], tools=[RUNBOOK_TOOL]),
        callable_agent("blast-radius", ["ESCALATE"]),
    )
    llm = FakeChatModel(
        responses=[final("ESCALATE", "x")], scripts={SUMMARIZER: list(script), BLAST: [explanation("ok")]}
    )
    response = await run(apps_dir, registry, llm, max_tool_rounds=2)

    assert response.decision == agent_pb2.ESCALATE
    assert response.reasons[0] == "x"
    if why is None:
        assert response.reasons[1].startswith("root-cause-summarizer: ")
    else:
        assert response.reasons[1] == f"orchestrator: callable 'root-cause-summarizer' didn't contribute ({why})."
    assert response.reasons[-1] == "blast-radius: ok"  # the other callable still contributes


async def test_a_slow_callable_times_out_without_holding_the_decision(apps_dir, app_with_callables):
    registry = app_with_callables(callable_agent("root-cause-summarizer", ["ESCALATE"]))
    llm = FakeChatModel(
        responses=[final("ESCALATE", "x")], scripts={SUMMARIZER: [explanation("late")]}, delays={SUMMARIZER: 5}
    )
    started = time.perf_counter()
    response = await run(apps_dir, registry, llm, callable_timeout_s=0.2)

    assert time.perf_counter() - started < 1
    assert list(response.reasons) == [
        "x", "orchestrator: callable 'root-cause-summarizer' didn't contribute (timed out after 0.2s).",
    ]


async def test_a_callable_with_a_missing_prompt_does_not_contribute(apps_dir, app_with_callables):
    agent = callable_agent("root-cause-summarizer", ["ESCALATE"])
    agent["prompt_ref"] = "prompts/missing.md"
    llm = FakeChatModel(responses=[final("ESCALATE", "x")])
    response = await run(apps_dir, app_with_callables(agent), llm)

    assert response.reasons[-1].startswith("orchestrator: callable 'root-cause-summarizer' didn't contribute (")
    assert "prompt_ref 'prompts/missing.md' is not a file" in response.reasons[-1]


async def test_an_unexpected_error_in_a_callable_is_logged_and_does_not_contribute(
    apps_dir, app_with_callables, monkeypatch
):
    async def boom(*args, **kwargs):
        raise KeyError("bug")

    monkeypatch.setattr("app.agent.graph.explain", boom)
    llm = FakeChatModel(responses=[final("ESCALATE", "x")])
    response = await run(apps_dir, app_with_callables(callable_agent("root-cause-summarizer", ["ESCALATE"])), llm)

    assert response.reasons[-1] == "orchestrator: callable 'root-cause-summarizer' didn't contribute (failed)."


# --- agent_id dispatch -----------------------------------------------------------

async def test_empty_agent_id_runs_the_entry_agent_with_the_whole_graph(apps_dir, app_with_callables):
    registry = app_with_callables(callable_agent("root-cause-summarizer", ["ESCALATE"]))
    llm = FakeChatModel(responses=[final("SUPPRESS", "flap", confidence=0.9)], scripts={SUMMARIZER: []})
    response = await run(apps_dir, registry, llm)

    assert response.agent_id == "triage-agent"
    assert response.decision == agent_pb2.SUPPRESS
    assert "alert-triage agent" in llm.calls[0]["system"]


async def test_callable_agent_id_runs_only_that_callable(apps_dir, app_with_callables):
    """Direct RunAgent on a callable: its reasons, unprefixed; no decision,
    no supervisor or guardrails, and no other callables."""
    registry = app_with_callables(
        callable_agent("root-cause-summarizer", ["ESCALATE"]),
        callable_agent("blast-radius", ["ESCALATE"]),
        escalate_when=[{"field": "alert.severity", "in": ["critical"]}],
    )
    llm = FakeChatModel(scripts={SUMMARIZER: [explanation("probable cause: none found")], BLAST: []})
    request = make_request()
    request.agent_id, request.severity = "root-cause-summarizer", "critical"
    response = await run(apps_dir, registry, llm, request)

    assert response.agent_id == "root-cause-summarizer"
    assert response.decision == agent_pb2.DECISION_UNSPECIFIED
    assert response.confidence == 0.0
    assert list(response.reasons) == ["probable cause: none found"]
    assert len(llm.calls) == 1 and "explanation agent" in llm.calls[0]["system"]
    assert "kind=triage_decision" not in llm.calls[0]["messages"][0].content


async def test_direct_callable_that_fails_says_so(apps_dir, app_with_callables):
    registry = app_with_callables(callable_agent("root-cause-summarizer", ["ESCALATE"]))
    request = make_request()
    request.agent_id = "root-cause-summarizer"
    response = await run(apps_dir, registry, FakeChatModel(scripts={SUMMARIZER: [text("??")]}), request)

    assert response.decision == agent_pb2.DECISION_UNSPECIFIED
    assert list(response.reasons) == [
        "orchestrator: no explanation (answer didn't parse: Agent's final answer is not JSON.)."
    ]
