"""`POST /alerts` (intake) and the Day 4 debug `GET /alerts/{alert_id}`.

Validation happens entirely before anything touches Kafka: envelope
(pydantic, 422), payload size (413), payload schema and `alert_key` (422).
"""

import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status

from app.core.apps import AppConfigError, FileAppStore
from app.core.envelope import AlertKeyError, build_alert_key, build_request, message_key
from app.kafka.decisions import DecisionTracker
from app.kafka.publisher import ALERT_RECEIVED, PublishError, Publisher
from app.models.alerts import AlertAccepted, AlertIn, AlertStatus

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/alerts", status_code=status.HTTP_202_ACCEPTED, response_model=AlertAccepted)
async def post_alert(alert: AlertIn, request: Request) -> AlertAccepted:
    state = request.app.state
    app_id: str = state.settings.default_app_id
    apps: FileAppStore = state.apps
    publisher: Publisher = state.publisher

    size = len(json.dumps(alert.payload, separators=(",", ":")).encode())
    if size > state.settings.max_payload_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"payload is {size} bytes; the limit is {state.settings.max_payload_bytes}.",
        )

    try:
        spec = apps.get(app_id)
    except AppConfigError:
        logger.exception("app %s is misconfigured", app_id)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"App '{app_id}' is misconfigured.") from None

    if errors := spec.payload_errors(alert.payload):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"message": "payload is invalid", "errors": errors})
    try:
        alert_key = build_alert_key(alert.payload, spec.alert_key_fields)
    except AlertKeyError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"message": str(e)}) from None

    alert_id = str(uuid.uuid4())
    received_at = datetime.now(UTC)
    message = build_request(app_id=app_id, alert_id=alert_id, alert_key=alert_key, alert=alert, received_at=received_at)
    # Tracked before publishing: the decision can arrive before publish() returns.
    tracker: DecisionTracker = state.tracker
    tracker.accepted(alert_id, app_id, alert_key, received_at)
    try:
        await publisher.publish(ALERT_RECEIVED, message_key(app_id, alert_key), message.SerializeToString())
    except PublishError as e:
        tracker.forget(alert_id)
        logger.warning("alert not accepted: %s", e)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Alert intake is temporarily unavailable; retry.",
            headers={"Retry-After": "5"},
        ) from None

    logger.info("accepted alert %s app_id=%s alert_key=%s", alert_id, app_id, alert_key)
    return AlertAccepted(alert_id=alert_id, app_id=app_id, alert_key=alert_key)


@router.get("/alerts/{alert_id}", response_model=AlertStatus)
async def get_alert(alert_id: str, request: Request) -> AlertStatus:
    """Debug only (Day 4): superseded by review-console on Day 9."""
    found = request.app.state.tracker.get(alert_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No alert '{alert_id}' known to this ingestion instance.")
    return found
