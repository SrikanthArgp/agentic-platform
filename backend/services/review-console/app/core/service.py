"""`CaseService`: cases in from `alert.decided`, verdicts out through `verdict_outbox`.

The state machine lives here, not in SQL: under the case's row lock, a
missing case is `CaseNotFoundError`, a resolved one is
`CaseAlreadyResolvedError`, and an open one is resolved and its
`verdict.recorded` event written to `verdict_outbox` in the same
transaction (ADR-0024). Both commit or neither does, so `cases` and
`memory-store` can't disagree about a verdict, and Kafka being down
doesn't stop an analyst: the `outbox-relay` worker publishes the row when
it can (ADR-0030).
"""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.core.cases import (
    Case,
    CaseAlreadyResolvedError,
    CaseNotFoundError,
    CaseStatus,
    NewCase,
    VerdictIn,
    case_from_decision,
    verdict_event,
)
from app.core.outbox import VERDICT_RECORDED, VerdictOutboxRow, message_key, trace_headers
from proto_gen import agent_pb2


@dataclass(frozen=True)
class CaseQuery:
    # Required: every read is scoped to one app (docs/ARCHITECTURE.md §8).
    app_id: str
    alert_key: str | None = None
    status: CaseStatus | None = None
    limit: int = 50
    offset: int = 0


class LockedCase(Protocol):
    """A case row held for update until the `lock()` block exits."""

    case: Case | None

    async def resolve(self, verdict: VerdictIn, at: datetime) -> Case: ...

    async def enqueue(self, row: VerdictOutboxRow) -> None:
        """Insert into `verdict_outbox` in the lock's transaction."""
        ...


class CaseStore(Protocol):
    async def add(self, case: NewCase) -> bool:
        """Insert; False if (app_id, alert_id) already has a case."""
        ...

    async def get(self, case_id: int) -> Case | None: ...

    async def list(self, query: CaseQuery) -> list[Case]:
        """Newest first."""
        ...

    def lock(self, case_id: int) -> AbstractAsyncContextManager[LockedCase]:
        """Commits on a clean exit, rolls back if the block raises.
        `StoreUnavailableError` if Postgres can't be reached or fails."""
        ...


class CaseService:
    def __init__(
        self,
        store: CaseStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._store = store
        self._clock = clock

    async def record_decision(self, response: agent_pb2.RunAgentResponse) -> bool | None:
        """None if the decision isn't a case; else whether a new case was created."""
        case = case_from_decision(response)
        if case is None:
            return None
        return await self._store.add(case)

    async def get(self, case_id: int) -> Case:
        case = await self._store.get(case_id)
        if case is None:
            raise CaseNotFoundError(case_id)
        return case

    async def list(self, query: CaseQuery) -> list[Case]:
        return await self._store.list(query)

    async def record_verdict(self, case_id: int, verdict: VerdictIn) -> Case:
        async with self._store.lock(case_id) as locked:
            if locked.case is None:
                raise CaseNotFoundError(case_id)
            if locked.case.status is CaseStatus.RESOLVED:
                raise CaseAlreadyResolvedError(locked.case)
            case = await locked.resolve(verdict, self._clock())
            await locked.enqueue(
                VerdictOutboxRow(
                    app_id=case.app_id,
                    case_id=case.id,
                    topic=VERDICT_RECORDED,
                    kafka_key=message_key(case.app_id, case.alert_key),
                    payload=json.dumps(verdict_event(case), separators=(",", ":")).encode(),
                    headers=trace_headers(),
                )
            )
        return case
