import json

import pytest

from app.agent.decision import MAX_REASON_CHARS, MAX_REASONS, DecisionParseError, parse_decision
from proto_gen import agent_pb2


@pytest.mark.parametrize("decision", ["AUTO_RESOLVE", "ESCALATE", "SUPPRESS"])
def test_parses_each_decision(decision):
    parsed = parse_decision(json.dumps({"decision": decision, "reasons": ["RB-001 matched"]}))
    assert parsed.decision == agent_pb2.Decision.Value(decision)
    assert parsed.reasons == ["RB-001 matched"]


def test_accepts_a_json_code_fence():
    parsed = parse_decision('```json\n{"decision": "SUPPRESS", "reasons": ["flap"]}\n```')
    assert parsed.decision == agent_pb2.SUPPRESS


def test_strips_drops_empty_and_caps_reasons():
    reasons = ["  a  ", "", "x" * (MAX_REASON_CHARS + 50), *[f"r{i}" for i in range(20)]]
    parsed = parse_decision(json.dumps({"decision": "ESCALATE", "reasons": reasons}))
    assert parsed.reasons[0] == "a"
    assert len(parsed.reasons[1]) == MAX_REASON_CHARS
    assert len(parsed.reasons) == MAX_REASONS


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "I think this should be escalated.",
        '{"decision": "ESCALATE"}',
        '{"decision": "ESCALATE", "reasons": []}',
        '{"decision": "ESCALATE", "reasons": ["  "]}',
        '{"decision": "DECISION_UNSPECIFIED", "reasons": ["x"]}',
        '{"decision": "escalate", "reasons": ["x"]}',
        '{"decision": "IGNORE", "reasons": ["x"]}',
        '["ESCALATE"]',
    ],
)
def test_rejects_anything_that_is_not_a_clean_decision(text):
    with pytest.raises(DecisionParseError):
        parse_decision(text)
