"""Window maths: (now - w, now], kinds, facts."""

from app.core.events import CONFIRMED_INCIDENT, CONFIRMED_NOISE, WINDOWS_MS, Event, Facts, aggregate, build_context, decision_event
from proto_gen import agent_pb2
from tests.conftest import H, NOW, decision


def counts(agg):
    return (agg.alert_count, agg.escalation_count, agg.suppression_count,
            agg.confirmed_incident_count, agg.confirmed_noise_count)


def test_event_exactly_at_the_window_edge_has_left_it():
    edge = Event("decision:ESCALATE", "a", NOW - WINDOWS_MS["1h"])
    just_inside = Event("decision:ESCALATE", "b", NOW - WINDOWS_MS["1h"] + 1)
    assert aggregate([edge], NOW, WINDOWS_MS["1h"]).alert_count == 0
    assert aggregate([just_inside], NOW, WINDOWS_MS["1h"]).alert_count == 1


def test_event_at_now_and_slightly_in_the_future_counts():
    events = [Event("decision:SUPPRESS", "a", NOW), Event("decision:SUPPRESS", "b", NOW + 500)]
    assert aggregate(events, NOW, WINDOWS_MS["1h"]).suppression_count == 2


def test_each_window_counts_only_its_span():
    events = [decision("ESCALATE", "a", 0.5), decision("SUPPRESS", "b", 3),
              decision("AUTO_RESOLVE", "c", 30), decision("SUPPRESS", "d", 24 * 7 + 1)]
    ctx = build_context("app", "k", events, Facts(seen=True), NOW)
    assert counts(ctx.window_1h) == (1, 1, 0, 0, 0)
    assert counts(ctx.window_24h) == (2, 1, 1, 0, 0)
    assert counts(ctx.window_7d) == (3, 1, 1, 0, 0)


def test_empty_history_is_all_zero_and_novel():
    ctx = build_context("app", "k", [], Facts(), NOW)
    for window in (ctx.window_1h, ctx.window_24h, ctx.window_7d):
        assert counts(window) == (0, 0, 0, 0, 0)
    assert ctx.is_novel_alert is True
    assert ctx.has_confirmed_incident_history is False
    assert (ctx.app_id, ctx.alert_key) == ("app", "k")


def test_auto_resolve_and_not_evaluated_count_as_alerts_only():
    events = [decision("AUTO_RESOLVE", "a", 1), decision("DECISION_UNSPECIFIED", "b", 1)]
    assert counts(aggregate(events, NOW, WINDOWS_MS["24h"])) == (2, 0, 0, 0, 0)


def test_verdicts_count_separately_from_alerts():
    events = [Event(CONFIRMED_INCIDENT, "case-1", NOW - H), Event(CONFIRMED_NOISE, "case-2", NOW - H)]
    assert counts(aggregate(events, NOW, WINDOWS_MS["24h"])) == (0, 0, 0, 1, 1)


def test_facts_from_events():
    assert Facts.from_events([]) == Facts()
    assert Facts.from_events([decision("SUPPRESS", "a", 1)]) == Facts(seen=True)
    assert Facts.from_events([Event(CONFIRMED_INCIDENT, "c", 0)]) == Facts(confirmed_incident=True)


def test_all_time_facts_survive_the_window():
    ctx = build_context("app", "k", [], Facts(seen=True, confirmed_incident=True), NOW)
    assert ctx.is_novel_alert is False
    assert ctx.has_confirmed_incident_history is True


def test_decision_event_from_response():
    response = agent_pb2.RunAgentResponse(app_id="a", alert_id="al-1", alert_key="k", decision=agent_pb2.SUPPRESS)
    assert decision_event(response, 123) == Event("decision:SUPPRESS", "al-1", 123)
