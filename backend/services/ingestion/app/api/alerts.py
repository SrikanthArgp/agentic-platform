"""`POST /apps/{app_id}/events`: alert intake.

The URL chooses the app; nothing infers it (docs/ARCHITECTURE.md §3).
Validation happens entirely before anything touches Kafka: envelope
(pydantic, 422), payload size (413), unknown app (404), payload against
that app's schema and `alert_key` (422).
"""

import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Path, Request, status

from app.core.apps import AppConfigError, AppStore, AppUnavailableError, UnknownAppError
from app.core.envelope import AlertKeyError, build_alert_key, build_request, message_key
from app.kafka.publisher import ALERT_RECEIVED, PublishError, Publisher
from app.models.alerts import AlertAccepted, AlertIn

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/apps/{app_id}/events", status_code=status.HTTP_202_ACCEPTED, response_model=AlertAccepted)
async def post_event(alert: AlertIn, request: Request, app_id: str = Path(max_length=63)) -> AlertAccepted:
    state = request.app.state
    apps: AppStore = state.apps
    publisher: Publisher = state.publisher

    size = len(json.dumps(alert.payload, separators=(",", ":")).encode())
    if size > state.settings.max_payload_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"payload is {size} bytes; the limit is {state.settings.max_payload_bytes}.",
        )

    try:
        spec = await apps.get(app_id)
    except UnknownAppError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
    except AppUnavailableError as e:
        logger.warning("app %s not resolvable: %s", app_id, e)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "App registry is temporarily unavailable; retry.",
            headers={"Retry-After": "5"},
        ) from None
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
    try:
        await publisher.publish(ALERT_RECEIVED, message_key(app_id, alert_key), message.SerializeToString())
    except PublishError as e:
        logger.warning("alert not accepted: %s", e)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Alert intake is temporarily unavailable; retry.",
            headers={"Retry-After": "5"},
        ) from None

    logger.info("accepted alert %s app_id=%s alert_key=%s", alert_id, app_id, alert_key)
    return AlertAccepted(alert_id=alert_id, app_id=app_id, alert_key=alert_key)
