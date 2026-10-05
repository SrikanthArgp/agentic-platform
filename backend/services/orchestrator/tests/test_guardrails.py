"""`escalate_when` evaluation, table-driven (ADR-0010, ADR-0021)."""

import pytest

from app.agent.guardrails import MISSING, Rule, equal, evaluate
from tests.conftest import context, make_request


def request(severity: str = "warning", **payload):
    r = make_request(**(payload or {"alert_type": "disk_full", "host": "web-01"}))
    r.severity = severity
    return r


CTX = context(
    incident_history=True,
    window_24h={"alert_count": 12, "escalation_count": 3, "suppression_count": 9},
)


@pytest.mark.parametrize(
    "rule, req, ctx, matched",
    [
        # alert.* — the envelope.
        (Rule("alert.severity", ["critical"]), request("critical"), CTX, True),
        (Rule("alert.severity", ["high", "critical"]), request("high"), CTX, True),
        (Rule("alert.severity", ["critical"]), request("warning"), CTX, False),
        (Rule("alert.severity", ["Critical"]), request("critical"), CTX, False),  # case-sensitive
        (Rule("alert.source", ["prometheus"]), request(), CTX, True),
        (Rule("alert.alert_key", ["disk_full:web-01"]), request(), CTX, True),
        (Rule("alert.alert_id", ["alert-1"]), request(), CTX, False),  # not an addressable envelope field
        (Rule("alert.payload", ["x"]), request(), CTX, False),
        # payload.* — nested paths, Struct numbers are doubles.
        (Rule("payload.alert_type", ["disk_full"]), request(), CTX, True),
        (Rule("payload.labels.team", ["payments"]), request(labels={"team": "payments"}), CTX, True),
        (Rule("payload.value", [97]), request(value=97), CTX, True),
        (Rule("payload.value", [97.0]), request(value=97), CTX, True),
        (Rule("payload.value", ["97"]), request(value=97), CTX, False),
        (Rule("payload.pii", [True]), request(pii=True), CTX, True),
        (Rule("payload.pii", [1]), request(pii=True), CTX, False),  # true is not 1
        (Rule("payload.count", [True]), request(count=1), CTX, False),  # 1 is not true
        # payload.* — missing or non-scalar -> no match.
        (Rule("payload.severity", ["critical"]), request("critical"), CTX, False),  # severity is envelope-only
        (Rule("payload.labels.team", ["x"]), request(labels={}), CTX, False),
        (Rule("payload.labels", ["x"]), request(labels={"a": "x"}), CTX, False),
        (Rule("payload.tags", ["x"]), request(tags=["x"]), CTX, False),
        (Rule("payload.host.name", ["web-01"]), request(), CTX, False),  # walks into a string
        # context.* — flags and window counts.
        (Rule("context.has_confirmed_incident_history", [True]), request(), CTX, True),
        (Rule("context.has_confirmed_incident_history", [True]), request(), context(), False),
        (Rule("context.is_novel_alert", [True]), request(), context(novel=True), True),
        (Rule("context.window_24h.escalation_count", [3, 4]), request(), CTX, True),
        (Rule("context.window_24h.escalation_count", [5]), request(), CTX, False),
        (Rule("context.window_1h.alert_count", [0]), request(), CTX, True),  # unset window reads as zeros
        (Rule("context.window_24h", [True]), request(), CTX, False),  # a message, not a scalar
        (Rule("context.no_such_field", [True]), request(), CTX, False),
        # context.* — no context at all never matches, even for "false"/0.
        (Rule("context.has_confirmed_incident_history", [False]), request(), None, False),
        (Rule("context.window_1h.alert_count", [0]), request(), None, False),
        # Unknown prefix / no path.
        (Rule("severity", ["critical"]), request("critical"), CTX, False),
        (Rule("headers.x", ["y"]), request(), CTX, False),
    ],
)
def test_rule_matching(rule, req, ctx, matched):
    assert bool(evaluate([rule], req, ctx)) is matched


def test_every_match_is_reported_in_manifest_order_and_names_its_rule():
    rules = [
        Rule("context.has_confirmed_incident_history", [True]),
        Rule("payload.alert_type", ["high_cpu"]),
        Rule("alert.severity", ["critical"]),
    ]
    matches = evaluate(rules, request("critical"), CTX)
    assert [m.index for m in matches] == [0, 2]
    assert [m.describe() for m in matches] == [
        "escalate_when[0] context.has_confirmed_incident_history = true",
        'escalate_when[2] alert.severity = "critical"',
    ]


def test_no_rules_no_matches():
    assert evaluate([], request("critical"), CTX) == []


@pytest.mark.parametrize(
    "value, allowed, expected",
    [
        ("a", "a", True), ("a", "A", False), (3, 3.0, True), (3.5, 3, False),
        (True, True, True), (False, False, True), (True, 1, False), (0, False, False), ("1", 1, False),
    ],
)
def test_equal_is_type_strict(value, allowed, expected):
    assert equal(value, allowed) is expected


def test_missing_has_a_readable_repr():
    assert repr(MISSING) == "<missing>"
