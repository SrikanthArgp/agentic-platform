"""A whole run with context, supervisor and guardrails (docs/ARCHITECTURE.md §5, plan.md Day 7).

Order: the agent's answer -> supervisor -> guardrails. Each step's reasons
follow the agent's own, in that order; steps 2-3 only move toward ESCALATE.
"""

import pytest

from proto_gen import agent_pb2
from tests.conftest import (
    APP_ID,
    KNOWN_CONTEXT,
    FakeChatModel,
    FakeMemory,
    FakeRegistry,
    context,
    final,
    make_request,
    make_runner,
    text,
    tool_call,
)

pytestmark = pytest.mark.anyio

CRITICAL = {"field": "alert.severity", "in": ["critical"]}
INCIDENT_HISTORY = {"field": "context.has_confirmed_incident_history", "in": [True]}


def registry(*rules, min_confidence=None) -> FakeRegistry:
    r = FakeRegistry()
    r.apps[APP_ID]["escalate_when"] = list(rules)
    if min_confidence is not None:
        r.apps[APP_ID]["supervisor"] = {"min_confidence": min_confidence}
    return r


def request(severity="warning"):
    r = make_request()
    r.severity = severity
    return r


async def run(apps_dir, answer, *, req=None, memory=None, reg=None, **kwargs):
    llm = FakeChatModel(responses=[answer])
    response = await make_runner(apps_dir, llm, memory=memory, registry=reg, **kwargs).run(req or request())
    return response, llm


async def test_memory_context_is_fetched_for_the_runs_key_and_shown_to_the_agent(apps_dir):
    memory = FakeMemory(context(window_24h={"alert_count": 12, "suppression_count": 12}))
    _, llm = await run(apps_dir, final("SUPPRESS"), memory=memory)

    assert memory.calls == [(APP_ID, "disk_full:web-01")]
    first_message = llm.calls[0]["messages"][0].content
    assert "kind=memory_context>>>" in first_message
    assert '"suppression_count": 12' in first_message


async def test_response_carries_the_supervised_confidence(apps_dir):
    response, _ = await run(apps_dir, final("SUPPRESS", confidence=0.85))
    assert response.decision == agent_pb2.SUPPRESS
    assert response.confidence == pytest.approx(0.85)
    assert list(response.reasons) == ["RB-001 says so"]


async def test_same_alert_decides_differently_on_known_vs_novel_history(apps_dir):
    """Day 7 DoD, in miniature: the agent says SUPPRESS both times; only the history differs."""
    noisy = context(window_24h={"alert_count": 12, "suppression_count": 12})
    known, _ = await run(apps_dir, final("SUPPRESS", "RB-006: flap"), memory=FakeMemory(noisy))
    novel, _ = await run(apps_dir, final("SUPPRESS", "RB-006: flap"), memory=FakeMemory(context(novel=True)))

    assert known.decision == agent_pb2.SUPPRESS
    assert novel.decision == agent_pb2.ESCALATE
    assert "first time this alert_key is seen" in novel.reasons[-1]


async def test_low_agent_confidence_escalates(apps_dir):
    response, _ = await run(apps_dir, final("AUTO_RESOLVE", "maybe transient", confidence=0.3))
    assert response.decision == agent_pb2.ESCALATE
    assert response.reasons[0] == "maybe transient"
    assert "orchestrator: supervisor: confidence 0.30 (the agent's own estimate)" in response.reasons[1]


async def test_manifest_threshold_overrides_the_platform_default(apps_dir):
    strict = registry(min_confidence=0.95)
    response, _ = await run(apps_dir, final("SUPPRESS", confidence=0.9), reg=strict)
    assert response.decision == agent_pb2.ESCALATE
    assert "threshold 0.95" in response.reasons[-1]

    lax, _ = await run(apps_dir, final("SUPPRESS", confidence=0.9), min_confidence=0.95, reg=registry(min_confidence=0.5))
    assert lax.decision == agent_pb2.SUPPRESS


async def test_guardrail_overrides_an_llm_suppress_and_names_the_rule(apps_dir):
    """Day 7 DoD: a critical alert is ESCALATEd though the agent (forced here) said SUPPRESS."""
    response, _ = await run(
        apps_dir, final("SUPPRESS", "RB-006: flap", confidence=0.95), req=request("critical"), reg=registry(CRITICAL)
    )
    assert response.decision == agent_pb2.ESCALATE
    assert list(response.reasons) == [
        "RB-006: flap",
        'orchestrator: guardrail escalate_when[0] alert.severity = "critical" matched; SUPPRESS changed to ESCALATE.',
    ]
    assert response.confidence == pytest.approx(0.95)  # the guardrail doesn't pretend the agent was unsure


async def test_guardrail_never_changes_an_escalate_but_still_names_the_match(apps_dir):
    response, _ = await run(apps_dir, final("ESCALATE", "disk >95%"), req=request("critical"), reg=registry(CRITICAL))
    assert response.decision == agent_pb2.ESCALATE
    assert response.reasons[-1] == (
        'orchestrator: guardrail escalate_when[0] alert.severity = "critical" matched (already ESCALATE).'
    )


async def test_guardrail_on_context_reads_the_memory_context(apps_dir):
    reg = registry(INCIDENT_HISTORY)
    history = context(incident_history=True, window_7d={"alert_count": 2})
    response, _ = await run(apps_dir, final("AUTO_RESOLVE"), memory=FakeMemory(history), reg=reg)
    assert response.decision == agent_pb2.ESCALATE
    assert "context.has_confirmed_incident_history = true matched" in response.reasons[-1]

    clean, _ = await run(apps_dir, final("AUTO_RESOLVE"), reg=reg)
    assert clean.decision == agent_pb2.AUTO_RESOLVE


async def test_non_matching_guardrails_change_nothing(apps_dir):
    response, _ = await run(apps_dir, final("SUPPRESS"), reg=registry(CRITICAL, INCIDENT_HISTORY))
    assert response.decision == agent_pb2.SUPPRESS
    assert list(response.reasons) == ["RB-001 says so"]


async def test_order_is_agent_then_supervisor_then_guardrails(apps_dir):
    """Low confidence AND a critical alert: the supervisor escalates first, the guardrail then sees ESCALATE."""
    response, _ = await run(
        apps_dir, final("SUPPRESS", "flap", confidence=0.2), req=request("critical"), reg=registry(CRITICAL)
    )
    assert response.decision == agent_pb2.ESCALATE
    assert response.reasons[0] == "flap"
    assert response.reasons[1].startswith("orchestrator: supervisor: confidence 0.20")
    assert response.reasons[2].endswith("matched (already ESCALATE).")
    assert len(response.reasons) == 3


async def test_memory_down_runs_without_context_and_escalates(apps_dir):
    reg = registry(INCIDENT_HISTORY)
    response, llm = await run(apps_dir, final("SUPPRESS", confidence=0.95), memory=FakeMemory(None), reg=reg)

    assert '"available": false' in llm.calls[0]["messages"][0].content
    assert response.decision == agent_pb2.ESCALATE
    assert response.confidence == pytest.approx(0.5)
    assert response.reasons[1] == (
        "orchestrator: memory context unavailable (UNAVAILABLE: connection refused); "
        "decided without this alert_key's history."
    )
    assert "capped at 0.50: no memory context" in response.reasons[2]
    # The context.* guardrail can't match without context; the supervisor is what escalated.
    assert not any("guardrail" in r for r in response.reasons)


async def test_unparseable_answer_has_zero_confidence_and_guardrails_still_name_matches(apps_dir):
    response, _ = await run(apps_dir, text("suppress it"), req=request("critical"), reg=registry(CRITICAL))
    assert response.decision == agent_pb2.ESCALATE
    assert response.confidence == 0.0
    assert "not JSON" in response.reasons[0]
    assert response.reasons[-1].endswith("matched (already ESCALATE).")


async def test_guardrails_apply_after_tool_calls_too(apps_dir):
    llm = FakeChatModel(responses=[tool_call(), final("AUTO_RESOLVE", "RB-001", confidence=0.9)])
    response = await make_runner(apps_dir, llm, registry=registry(CRITICAL)).run(request("critical"))
    assert response.decision == agent_pb2.ESCALATE
    assert [c.tool_name for c in response.tool_calls] == ["lookup_runbook"]


def test_known_context_is_not_novel():
    """The default FakeMemory context must not trigger a cap, or every other test would escalate."""
    assert not KNOWN_CONTEXT.is_novel_alert and not KNOWN_CONTEXT.has_confirmed_incident_history
