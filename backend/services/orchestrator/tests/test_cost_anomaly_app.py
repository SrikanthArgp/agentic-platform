"""`cost-anomaly-triage` as shipped (Day 17): its real manifest and prompt
from `backend/apps/`, a scripted model, and the seeded history `seed.py`
writes (three monthly CONFIRMED_NOISE cases on checkout-svc:us-east-1).

The model is scripted, so these tests don't show the LLM *choosing*
SUPPRESS (that's the live check); they show everything around it: what the
agent is told and given, and that the platform keeps a SUPPRESS only when
nothing deterministic says otherwise.
"""

import json
from typing import Any

import pytest
import yaml
from google.protobuf.struct_pb2 import Struct

from app.core.config import DEFAULT_APPS_DIR
from app.tools.gateway import ToolResult
from proto_gen import agent_pb2, memory_store_pb2
from tests.conftest import FakeChatModel, FakeGateway, FakeMemory, FakeRegistry, final, make_runner, tool_call

pytestmark = pytest.mark.anyio

APP = "cost-anomaly-triage"
KEY = "checkout-svc:us-east-1"
MANIFEST = yaml.safe_load((DEFAULT_APPS_DIR / APP / "manifest.yaml").read_text())
PROMPT = (DEFAULT_APPS_DIR / APP / MANIFEST["agents"][0]["prompt_ref"]).read_text()


def resolved_manifest() -> dict[str, Any]:
    """`GET /apps/cost-anomaly-triage` as registry serves it: each agent's
    allowlisted tools resolved."""
    app = json.loads(json.dumps(MANIFEST))
    for agent in app["agents"]:
        agent.setdefault("invoke_on", [])
        agent["tools"] = [
            {"tool_id": t, "version": "1.0.0", "scope": "global" if t == "similar-past-case-lookup" else "app",
             "description": t, "input_schema": {"type": "object"}}
            for t in agent["tool_allowlist"]
        ]
    return app


# What similar-past-case-lookup returns once seed.py has run.
SEEDED_PAST_CASES = {
    "found": True,
    "alert_key": KEY,
    "cases": [
        {"case_id": 103 - i, "decided_at": f"2026-{m:02d}-01T06:00:00Z", "decision": "ESCALATE",
         "reasons": [f"EMR cluster emr-recon-2026{m:02d} tagged job: monthly-reconciliation, schedule: monthly"],
         "verdict": "CONFIRMED_NOISE", "resolved_at": f"2026-{m:02d}-01T09:00:00Z",
         "resolution_notes": "Expected: finance-data's monthly reconciliation EMR batch runs on the 1st."}
        for i, m in enumerate((9, 8, 7))
    ],
}
BILLING = {"service": "checkout-svc", "region": "us-east-1", "total_cost_usd": 1568.05,
           "by_resource": [{"resource_id": "emr-recon-202610", "cost_usd": 1146.9,
                            "tags": {"owner": "finance-data", "job": "monthly-reconciliation",
                                     "schedule": "monthly"}}]}


def spike(severity: str = "warning", **extra: Any) -> agent_pb2.RunAgentRequest:
    payload = Struct()
    payload.update({"account": "123456789012", "service": "checkout-svc", "region": "us-east-1",
                    "window_start": "2026-10-01T00:00:00Z", "window_end": "2026-10-02T00:00:00Z",
                    "baseline_cost": 421.15, "observed_cost": 1568.05, **extra})
    return agent_pb2.RunAgentRequest(app_id=APP, alert_id="spike-1", alert_key=KEY, source="cost-anomaly-detector",
                                     severity=severity, message="checkout-svc spend 3.7x baseline", payload=payload)


def memory(*, incident_history: bool = False, noise_history: bool = True) -> FakeMemory:
    # Monthly runs are older than memory's 7 days: seen before, empty
    # windows, and (from the seeded verdicts) an all-time noise fact.
    return FakeMemory(memory_store_pb2.GetContextResponse(
        app_id=APP, alert_key=KEY, is_novel_alert=False, has_confirmed_incident_history=incident_history,
        has_confirmed_noise_history=noise_history))


def gateway() -> FakeGateway:
    return FakeGateway(results={"similar-past-case-lookup": ToolResult(False, SEEDED_PAST_CASES),
                                "billing-lookup": ToolResult(False, BILLING)})


def suppressing_model() -> FakeChatModel:
    return FakeChatModel(responses=[
        tool_call("billing-lookup", service="checkout-svc", region="us-east-1",
                  window_start="2026-10-01T00:00:00Z", window_end="2026-10-02T00:00:00Z"),
        final("SUPPRESS", "$1,146.90 of $1,568.05 is emr-recon-202610 (job: monthly-reconciliation, "
              "schedule: monthly)", "cases 103, 102, 101: CONFIRMED_NOISE for the same job on the 1st of Sep, Aug, Jul",
              confidence=0.85),
    ])


async def run(request, llm, *, mem=None, gw=None):
    gw = gw or gateway()
    runner = make_runner(DEFAULT_APPS_DIR, llm, gw, registry=FakeRegistry({APP: resolved_manifest()}),
                         memory=mem or memory())
    return await runner.run(request), gw


def test_manifest_requires_a_noise_verdict_before_a_suppress():
    assert MANIFEST["supervisor"] == {"min_confidence": 0.6, "suppress_requires_noise_verdict": True}


def test_manifest_has_both_guardrails_and_the_new_agent_version():
    assert {(r["field"], tuple(r["in"])) for r in MANIFEST["escalate_when"]} == {
        ("alert.severity", ("critical",)), ("payload.anomaly_level", ("extreme",)),
    }
    assert MANIFEST["agents"][0]["version"] == "0.2.0"


def test_prompt_suppresses_only_on_confirmed_noise_past_cases_not_tags_or_memory():
    assert "at least two past cases" in PROMPT and "`CONFIRMED_NOISE`" in PROMPT
    assert "Tags alone never justify SUPPRESS" in PROMPT
    assert "memory context covers 7 days at most" in PROMPT


async def test_agent_gets_the_real_prompt_and_the_seeded_past_cases_as_data():
    llm = suppressing_model()
    await run(spike(), llm)

    first = llm.calls[0]
    assert "Tags alone never justify SUPPRESS" in first["system"]
    opening = first["messages"][0].content
    assert "similar-past-case-lookup" in opening
    for case_id in (103, 102, 101):
        assert f'"case_id": {case_id}' in opening
    assert "monthly reconciliation" in opening


async def test_recurring_spike_suppression_is_kept_when_nothing_deterministic_objects():
    response, gw = await run(spike(), suppressing_model())

    assert response.decision == agent_pb2.SUPPRESS
    assert any("cases 103, 102, 101" in r for r in response.reasons)
    assert [c[0] for c in gw.calls] == ["similar-past-case-lookup", "billing-lookup"]
    assert response.confidence == pytest.approx(0.85)


async def test_extreme_anomaly_level_escalates_despite_the_recurring_history():
    response, _ = await run(spike(anomaly_level="extreme"), suppressing_model())

    assert response.decision == agent_pb2.ESCALATE
    assert any('escalate_when[1] payload.anomaly_level = "extreme" matched; SUPPRESS changed to ESCALATE' in r
               for r in response.reasons)


@pytest.mark.parametrize("level", ["low", "high"])
async def test_lower_anomaly_levels_dont_trigger_the_guardrail(level):
    response, _ = await run(spike(anomaly_level=level), suppressing_model())
    assert response.decision == agent_pb2.SUPPRESS


async def test_critical_severity_escalates():
    response, _ = await run(spike(severity="critical"), suppressing_model())
    assert response.decision == agent_pb2.ESCALATE
    assert any('escalate_when[0] alert.severity = "critical" matched' in r for r in response.reasons)


async def test_tags_only_suppress_on_a_key_no_analyst_marked_as_noise_is_escalated():
    """What live testing caught (Day 17): a repeat spike, so not novel, with
    schedule tags and no verdicts; the agent suppresses anyway. The opt-in
    supervisor rule (ADR-0031) escalates it deterministically."""
    response, _ = await run(spike(), suppressing_model(), mem=memory(noise_history=False))

    assert response.decision == agent_pb2.ESCALATE
    assert response.confidence == pytest.approx(0.3)
    assert any("no analyst has ever confirmed noise on this alert_key" in r for r in response.reasons)


async def test_confirmed_incident_history_overrides_a_suppress():
    response, _ = await run(spike(), suppressing_model(), mem=memory(incident_history=True))
    assert response.decision == agent_pb2.ESCALATE
