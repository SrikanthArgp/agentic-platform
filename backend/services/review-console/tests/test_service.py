"""CaseService: the persistence filter and the verdict state machine."""

import asyncio
import json

import pytest

from app.core.cases import CaseAlreadyResolvedError, CaseNotFoundError, CaseStatus, Verdict, VerdictIn
from app.kafka.publisher import PublishError
from proto_gen import agent_pb2
from tests.conftest import APP_ID, KEY, T0, alert, decided

pytestmark = pytest.mark.anyio

NOISE = VerdictIn(verdict=Verdict.CONFIRMED_NOISE, verdict_by="ana")


async def test_escalate_becomes_an_open_case_with_its_reasoning(service, store):
    assert await service.record_decision(decided()) is True
    [case] = store.cases.values()
    assert (case.app_id, case.alert_id, case.alert_key, case.agent_id) == (APP_ID, "al-1", KEY, "triage-agent")
    assert (case.decision, case.status, case.confidence) == ("ESCALATE", CaseStatus.OPEN, 0.55)
    assert case.reasons == ["disk at 97% on web-01", "runbook says escalate above 95%"]
    assert [(c.agent_id, c.tool_name) for c in case.tool_calls] == [
        ("triage-agent", "lookup_runbook"),
        ("root-cause-summarizer", "recent-changes-lookup"),
    ]
    assert (case.alert.source, case.alert.severity, case.alert.message) == (
        "prometheus", "critical", "Disk 97% full on web-01",
    )
    assert case.alert.fired_at == T0
    assert case.alert.payload == {"alert_type": "disk_full", "host": "web-01", "value": 97.0}


async def test_a_decision_without_the_alert_still_becomes_a_case(service, store):
    """Published before RunAgentResponse carried it (ADR-0023)."""
    response = decided()
    response.ClearField("alert")
    assert await service.record_decision(response) is True
    assert store.cases[1].alert is None


async def test_alert_without_a_timestamp_has_no_fired_at(service, store):
    await service.record_decision(decided(alert=alert(timestamp_unix_ms=0)))
    assert store.cases[1].alert.fired_at is None


@pytest.mark.parametrize("decision", [agent_pb2.AUTO_RESOLVE, agent_pb2.SUPPRESS, agent_pb2.DECISION_UNSPECIFIED])
async def test_other_decisions_are_not_persisted(service, store, decision):
    assert await service.record_decision(decided(decision=decision)) is None
    assert store.cases == {}


async def test_duplicate_decision_creates_one_case(service, store):
    assert await service.record_decision(decided()) is True
    assert await service.record_decision(decided()) is False
    assert len(store.cases) == 1


async def test_verdict_resolves_the_case_and_publishes_once(service, store, publisher):
    await service.record_decision(decided())
    case = await service.record_verdict(1, NOISE)
    assert (case.status, case.verdict, case.verdict_by) == (CaseStatus.RESOLVED, Verdict.CONFIRMED_NOISE, "ana")
    assert case.resolved_at is not None
    [(topic, key, value)] = publisher.sent
    assert (topic, key) == ("verdict.recorded", f"{APP_ID}:{KEY}".encode())
    assert json.loads(value) == {
        "app_id": APP_ID,
        "case_id": 1,
        "alert_key": KEY,
        "verdict": "CONFIRMED_NOISE",
        "verdict_by": "ana",
        "recorded_at": case.resolved_at.isoformat(),
    }


async def test_second_verdict_is_rejected_and_the_first_stands(service, store, publisher):
    await service.record_decision(decided())
    await service.record_verdict(1, NOISE)
    with pytest.raises(CaseAlreadyResolvedError) as e:
        await service.record_verdict(1, VerdictIn(verdict=Verdict.CONFIRMED_INCIDENT, verdict_by="bo"))
    assert (e.value.case.verdict, e.value.case.verdict_by) == (Verdict.CONFIRMED_NOISE, "ana")
    assert store.cases[1].verdict is Verdict.CONFIRMED_NOISE
    assert len(publisher.sent) == 1


async def test_concurrent_verdicts_resolve_exactly_once(service, store, publisher):
    await service.record_decision(decided())
    results = await asyncio.gather(
        service.record_verdict(1, NOISE),
        service.record_verdict(1, VerdictIn(verdict=Verdict.CONFIRMED_INCIDENT, verdict_by="bo")),
        return_exceptions=True,
    )
    assert sum(isinstance(r, CaseAlreadyResolvedError) for r in results) == 1
    assert len(publisher.sent) == 1


async def test_publish_failure_leaves_the_case_open_for_a_retry(service, store, publisher):
    await service.record_decision(decided())
    publisher.down = True
    with pytest.raises(PublishError):
        await service.record_verdict(1, NOISE)
    assert store.cases[1].status is CaseStatus.OPEN and store.cases[1].verdict is None

    publisher.down = False
    assert (await service.record_verdict(1, NOISE)).status is CaseStatus.RESOLVED


async def test_verdict_on_unknown_case(service, publisher):
    with pytest.raises(CaseNotFoundError):
        await service.record_verdict(99, NOISE)
    assert publisher.sent == []


@pytest.mark.parametrize("notes", [None, "rotated logs, raised disk alert threshold on web-01"])
async def test_verdict_with_and_without_notes_and_notes_never_published(service, store, publisher, notes):
    await service.record_decision(decided())
    case = await service.record_verdict(1, VerdictIn(verdict=Verdict.CONFIRMED_INCIDENT, verdict_by="ana", resolution_notes=notes))
    assert case.resolution_notes == notes
    assert "resolution_notes" not in json.loads(publisher.sent[0][2])
