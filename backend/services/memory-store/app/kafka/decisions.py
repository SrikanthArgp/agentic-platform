"""`alert.decided` -> one decision event per alert (docs/adr/0016).

A decision's time is the Kafka message timestamp (when `orchestrator`
published it). A redelivered message is stored once (unique `alert_id`).
An undecodable message, or one missing `app_id`/`alert_id`/`alert_key`, is
logged and skipped; `consumer.py` retries anything else.
"""

import logging

from google.protobuf.message import DecodeError

from app.core.events import decision_event
from app.core.service import MemoryService
from proto_gen import agent_pb2

logger = logging.getLogger(__name__)

ALERT_DECIDED = "alert.decided"


async def handle_decided(raw: bytes, timestamp_ms: int, service: MemoryService) -> bool:
    """Record one `alert.decided` message. False if it was skipped as unusable."""
    response = agent_pb2.RunAgentResponse()
    try:
        response.ParseFromString(raw)
    except DecodeError:
        logger.error("skipping undecodable alert.decided message (%d bytes)", len(raw))
        return False
    if not (response.app_id and response.alert_id and response.alert_key):
        logger.error("skipping alert.decided without app_id/alert_id/alert_key (alert_id=%r)", response.alert_id)
        return False
    inserted = await service.record(response.app_id, response.alert_key, decision_event(response, timestamp_ms))
    logger.info(
        "recorded decision app_id=%s alert_key=%s alert_id=%s decision=%s%s",
        response.app_id, response.alert_key, response.alert_id,
        agent_pb2.Decision.Name(response.decision), "" if inserted else " (duplicate)",
    )
    return True

