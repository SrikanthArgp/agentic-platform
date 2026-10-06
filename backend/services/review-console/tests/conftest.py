"""Fakes for unit tests: an in-memory case store and a recording publisher."""

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from google.protobuf.struct_pb2 import Struct

from app.core.cases import Case, CaseStatus, NewCase, VerdictIn
from app.core.service import CaseQuery, CaseService
from app.kafka.publisher import PublishError
from proto_gen import agent_pb2

APP_ID = "test-app"
KEY = "disk_full:web-01"
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def alert(**overrides) -> agent_pb2.RunAgentRequest:
    payload = Struct()
    payload.update({"alert_type": "disk_full", "host": "web-01", "value": 97})
    fields = dict(
        app_id=APP_ID, alert_id="al-1", alert_key=KEY, source="prometheus", severity="critical",
        message="Disk 97% full on web-01", timestamp_unix_ms=int(T0.timestamp() * 1000), payload=payload,
    )
    fields.update(overrides)
    return agent_pb2.RunAgentRequest(**fields)


def decided(**overrides) -> agent_pb2.RunAgentResponse:
    fields = dict(
        app_id=APP_ID,
        agent_id="triage-agent",
        alert_id="al-1",
        alert_key=KEY,
        decision=agent_pb2.ESCALATE,
        reasons=["disk at 97% on web-01", "runbook says escalate above 95%"],
        tool_calls=[
            agent_pb2.ToolCall(
                tool_name="lookup_runbook", result_summary="disk_full: escalate >95%", agent_id="triage-agent"
            ),
            agent_pb2.ToolCall(
                tool_name="recent-changes-lookup", result_summary="[]", agent_id="root-cause-summarizer"
            ),
        ],
        confidence=0.55,
        alert=alert(),
    )
    fields.update(overrides)
    return agent_pb2.RunAgentResponse(**fields)


class _Locked:
    def __init__(self, store: "FakeStore", case: Case | None):
        self._store = store
        self.case = case

    async def resolve(self, verdict: VerdictIn, at: datetime) -> Case:
        assert self.case is not None
        self.case = self._store.cases[self.case.id] = self.case.model_copy(
            update={
                "status": CaseStatus.RESOLVED,
                "verdict": verdict.verdict,
                "verdict_by": verdict.verdict_by,
                "resolution_notes": verdict.resolution_notes,
                "resolved_at": at,
            }
        )
        return self.case


class FakeStore:
    """Mirrors PostgresCaseStore: unique (app_id, alert_id), newest first,
    and `lock()` serialises verdicts and rolls back on an exception."""

    def __init__(self) -> None:
        self.cases: dict[int, Case] = {}
        self._lock = asyncio.Lock()

    async def add(self, case: NewCase) -> bool:
        if any((c.app_id, c.alert_id) == (case.app_id, case.alert_id) for c in self.cases.values()):
            return False
        case_id = len(self.cases) + 1
        self.cases[case_id] = Case(
            **case.model_dump(), id=case_id, status=CaseStatus.OPEN, created_at=T0 + timedelta(minutes=case_id)
        )
        return True

    async def get(self, case_id: int) -> Case | None:
        return self.cases.get(case_id)

    async def list(self, query: CaseQuery) -> list[Case]:
        found = [
            c for c in self.cases.values()
            if c.app_id == query.app_id
            and query.alert_key in (None, c.alert_key)
            and query.status in (None, c.status)
        ]
        found.sort(key=lambda c: (c.created_at, c.id), reverse=True)
        return found[query.offset : query.offset + query.limit]

    @asynccontextmanager
    async def lock(self, case_id: int) -> AsyncIterator[_Locked]:
        async with self._lock:
            snapshot = copy.deepcopy(self.cases)
            try:
                yield _Locked(self, self.cases.get(case_id))
            except BaseException:
                self.cases = snapshot
                raise


class FakePublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []
        self.down = False

    async def publish(self, topic: str, key: bytes, value: bytes) -> None:
        # Yield, as a real send does, so concurrent verdicts interleave.
        await asyncio.sleep(0)
        if self.down:
            raise PublishError("Kafka down")
        self.sent.append((topic, key, value))


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def service(store, publisher) -> CaseService:
    return CaseService(store, publisher, clock=lambda: T0 + timedelta(hours=1))
