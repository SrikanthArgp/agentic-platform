"""Day 4 debug only: remember accepted alerts and their `alert.decided` results.

Backs the throwaway `GET /alerts/{id}`, so a decision is observable before
`review-console` exists; delete both on Day 9. In-memory and per-replica on
purpose: nothing durable should grow around it. The consumer has no group
and reads `alert.decided` from the start, so a restart rebuilds what it can
(decisions without an accept time on this replica have no latency).
"""

import asyncio
import logging
from collections import OrderedDict
from datetime import UTC, datetime

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError
from google.protobuf.message import DecodeError

from app.models.alerts import AlertStatus
from proto_gen import agent_pb2

logger = logging.getLogger(__name__)

ALERT_DECIDED = "alert.decided"
MAX_TRACKED = 10_000
RECONNECT_DELAY_S = 2.0


class DecisionTracker:
    def __init__(self, max_tracked: int = MAX_TRACKED):
        self._alerts: OrderedDict[str, AlertStatus] = OrderedDict()
        self._max = max_tracked

    def accepted(self, alert_id: str, app_id: str, alert_key: str, at: datetime) -> None:
        self._put(AlertStatus(alert_id=alert_id, app_id=app_id, alert_key=alert_key, status="pending", accepted_at=at))

    def decided(self, response: agent_pb2.RunAgentResponse, at: datetime) -> None:
        known = self._alerts.get(response.alert_id)
        accepted_at = known.accepted_at if known else at
        self._put(
            AlertStatus(
                alert_id=response.alert_id,
                app_id=response.app_id,
                alert_key=known.alert_key if known else response.alert_key,
                status="decided",
                accepted_at=accepted_at,
                decided_at=at,
                latency_ms=int((at - accepted_at).total_seconds() * 1000) if known else None,
                decision=agent_pb2.Decision.Name(response.decision),
                agent_id=response.agent_id,
                reasons=list(response.reasons),
                tool_calls=[{"tool_name": c.tool_name, "result_summary": c.result_summary} for c in response.tool_calls],
            )
        )

    def forget(self, alert_id: str) -> None:
        self._alerts.pop(alert_id, None)

    def get(self, alert_id: str) -> AlertStatus | None:
        return self._alerts.get(alert_id)

    def _put(self, status: AlertStatus) -> None:
        self._alerts[status.alert_id] = status
        self._alerts.move_to_end(status.alert_id)
        while len(self._alerts) > self._max:
            self._alerts.popitem(last=False)

    def handle_message(self, raw: bytes) -> None:
        try:
            response = agent_pb2.RunAgentResponse.FromString(raw)
        except DecodeError:
            logger.warning("skipping undecodable alert.decided message")
            return
        self.decided(response, datetime.now(UTC))


class DecisionConsumer:
    def __init__(self, tracker: DecisionTracker, bootstrap_servers: str):
        self._tracker = tracker
        self._bootstrap_servers = bootstrap_servers
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever(), name="decision-consumer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        while True:
            consumer = AIOKafkaConsumer(
                ALERT_DECIDED, bootstrap_servers=self._bootstrap_servers, auto_offset_reset="earliest"
            )
            try:
                await consumer.start()
                async for msg in consumer:
                    self._tracker.handle_message(msg.value)
            except KafkaError as e:
                logger.warning("alert.decided consumer: Kafka unavailable (%s); retrying", e)
                await asyncio.sleep(RECONNECT_DELAY_S)
            finally:
                await consumer.stop()
