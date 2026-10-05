"""The supervisor step, table-driven (docs/adr/0020)."""

import pytest

from app.agent.supervisor import caps, supervise
from proto_gen import agent_pb2
from tests.conftest import KNOWN_CONTEXT, context

A, E, S = agent_pb2.AUTO_RESOLVE, agent_pb2.ESCALATE, agent_pb2.SUPPRESS
NOVEL = context(novel=True)
INCIDENT = context(incident_history=True, window_7d={"alert_count": 4})
NO_CONTEXT = None


@pytest.mark.parametrize(
    "decision, agent_conf, ctx, threshold, want_decision, want_conf",
    [
        # Agent's own estimate vs. threshold, nothing capped.
        (S, 0.9, KNOWN_CONTEXT, 0.6, S, 0.9),
        (A, 0.6, KNOWN_CONTEXT, 0.6, A, 0.6),  # equal to the threshold passes
        (A, 0.59, KNOWN_CONTEXT, 0.6, E, 0.59),
        (S, 0.0, KNOWN_CONTEXT, 0.6, E, 0.0),
        (S, 0.95, KNOWN_CONTEXT, 0.97, E, 0.95),  # an app's stricter threshold
        (S, 0.1, KNOWN_CONTEXT, 0.0, S, 0.1),  # threshold 0: supervisor off
        # ESCALATE is never changed, however low.
        (E, 0.0, KNOWN_CONTEXT, 0.6, E, 0.0),
        (E, 0.9, NOVEL, 0.6, E, 0.9),
        # Novel alert_key caps non-ESCALATE at 0.5.
        (A, 0.95, NOVEL, 0.6, E, 0.5),
        (S, 0.95, NOVEL, 0.6, E, 0.5),
        (S, 0.95, NOVEL, 0.4, S, 0.5),  # a lax app's threshold lets it through
        # Confirmed incident history caps SUPPRESS at 0.3, not AUTO_RESOLVE.
        (S, 0.95, INCIDENT, 0.6, E, 0.3),
        (S, 0.95, INCIDENT, 0.3, S, 0.3),
        (A, 0.95, INCIDENT, 0.6, A, 0.95),
        # No memory context caps everything at 0.5.
        (A, 0.95, NO_CONTEXT, 0.6, E, 0.5),
        (E, 0.95, NO_CONTEXT, 0.6, E, 0.5),
        # The lowest of the agent's value and every cap wins.
        (S, 0.2, NOVEL, 0.1, S, 0.2),
        (S, 0.9, context(novel=True, incident_history=True), 0.6, E, 0.3),
    ],
)
def test_confidence_threshold_routing(decision, agent_conf, ctx, threshold, want_decision, want_conf):
    result = supervise(decision, agent_conf, ctx, threshold)
    assert (result.decision, result.confidence) == (want_decision, pytest.approx(want_conf))
    assert (result.reason is not None) == (want_decision != decision)


def test_reason_names_the_value_the_threshold_and_the_cap():
    result = supervise(S, 0.95, NOVEL, 0.6)
    assert result.reason == (
        "supervisor: confidence 0.50 (capped at 0.50: first time this alert_key is seen) is below this "
        "app's threshold 0.60; SUPPRESS changed to ESCALATE for human review."
    )


def test_reason_says_when_the_agent_itself_was_unsure():
    result = supervise(A, 0.4, KNOWN_CONTEXT, 0.6)
    assert "confidence 0.40 (the agent's own estimate)" in result.reason
    assert "AUTO_RESOLVE changed to ESCALATE" in result.reason


def test_caps_are_only_those_that_apply():
    assert caps(A, KNOWN_CONTEXT) == []
    assert [c for c, _ in caps(S, context(novel=True, incident_history=True))] == [0.5, 0.3]
    assert [c for c, _ in caps(E, context(novel=True, incident_history=True))] == []
    assert [c for c, _ in caps(E, None)] == [0.5]
