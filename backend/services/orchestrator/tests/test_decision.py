import json

import pytest

from app.agent.decision import MAX_REASON_CHARS, MAX_REASONS, DecisionParseError, parse_decision
from proto_gen import agent_pb2


@pytest.mark.parametrize("decision", ["AUTO_RESOLVE", "ESCALATE", "SUPPRESS"])
def test_parses_each_decision(decision):
    parsed = parse_decision(json.dumps({"decision": decision, "confidence": 0.8, "reasons": ["RB-001 matched"]}))
    assert parsed.decision == agent_pb2.Decision.Value(decision)
    assert parsed.confidence == 0.8
    assert parsed.reasons == ["RB-001 matched"]


@pytest.mark.parametrize("confidence", [0, 1, 0.0, 0.55, 1.0])
def test_accepts_confidence_from_0_to_1(confidence):
    parsed = parse_decision(json.dumps({"decision": "SUPPRESS", "confidence": confidence, "reasons": ["x"]}))
    assert parsed.confidence == confidence


def test_accepts_a_json_code_fence():
    parsed = parse_decision('```json\n{"decision": "SUPPRESS", "confidence": 0.7, "reasons": ["flap"]}\n```')
    assert parsed.decision == agent_pb2.SUPPRESS


def test_strips_drops_empty_and_caps_reasons():
    reasons = ["  a  ", "", "x" * (MAX_REASON_CHARS + 50), *[f"r{i}" for i in range(20)]]
    parsed = parse_decision(json.dumps({"decision": "ESCALATE", "confidence": 0.9, "reasons": reasons}))
    assert parsed.reasons[0] == "a"
    assert len(parsed.reasons[1]) == MAX_REASON_CHARS
    assert len(parsed.reasons) == MAX_REASONS


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "I think this should be escalated.",
        '{"decision": "ESCALATE", "confidence": 0.9}',
        '{"decision": "ESCALATE", "confidence": 0.9, "reasons": []}',
        '{"decision": "ESCALATE", "confidence": 0.9, "reasons": ["  "]}',
        '{"decision": "DECISION_UNSPECIFIED", "confidence": 0.9, "reasons": ["x"]}',
        '{"decision": "escalate", "confidence": 0.9, "reasons": ["x"]}',
        '{"decision": "IGNORE", "confidence": 0.9, "reasons": ["x"]}',
        # confidence: required, a number, 0-1.
        '{"decision": "SUPPRESS", "reasons": ["x"]}',
        '{"decision": "SUPPRESS", "confidence": null, "reasons": ["x"]}',
        '{"decision": "SUPPRESS", "confidence": "0.9", "reasons": ["x"]}',
        '{"decision": "SUPPRESS", "confidence": true, "reasons": ["x"]}',
        '{"decision": "SUPPRESS", "confidence": 1.01, "reasons": ["x"]}',
        '{"decision": "SUPPRESS", "confidence": -0.1, "reasons": ["x"]}',
        '["ESCALATE"]',
    ],
)
def test_rejects_anything_that_is_not_a_clean_decision(text):
    with pytest.raises(DecisionParseError):
        parse_decision(text)
