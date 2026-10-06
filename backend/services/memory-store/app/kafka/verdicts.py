"""`verdict.recorded` -> one verdict event per case (plan.md Day 10, ADR-0017).

`review-console` publishes `{app_id, case_id, alert_key, verdict,
verdict_by, recorded_at}` as JSON (docs/ARCHITECTURE.md §9). Each becomes
a `verdict:CONFIRMED_INCIDENT` / `verdict:CONFIRMED_NOISE` event with
`ref_id` = the case id, so a redelivered verdict counts once. Like a
decision, its time is the Kafka message timestamp: `review-console`
publishes inside the verdict's transaction, so it's the moment of the
verdict, and both event kinds are timed by the same clock.

`verdict_by` and `recorded_at` aren't stored: memory holds counts and
flags, not who said what (ADR-0011).

An unusable message (not JSON, a missing field, an unknown verdict) is
logged and skipped; the consumer retries anything else.
"""

import json
import logging

from app.core.events import verdict_event
from app.core.service import MemoryService

logger = logging.getLogger(__name__)

VERDICT_RECORDED = "verdict.recorded"


async def handle_verdict(raw: bytes, timestamp_ms: int, service: MemoryService) -> bool:
    """Record one `verdict.recorded` message. False if it was skipped as unusable."""
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.error("skipping undecodable verdict.recorded message (%d bytes)", len(raw))
        return False
    if not isinstance(body, dict):
        logger.error("skipping verdict.recorded that isn't a JSON object")
        return False
    app_id, alert_key, case_id = body.get("app_id"), body.get("alert_key"), body.get("case_id")
    if not (isinstance(app_id, str) and app_id and isinstance(alert_key, str) and alert_key
            and isinstance(case_id, int | str) and not isinstance(case_id, bool) and str(case_id)):
        logger.error("skipping verdict.recorded without app_id/alert_key/case_id (case_id=%r)", case_id)
        return False
    try:
        event = verdict_event(str(case_id), str(body.get("verdict")), timestamp_ms)
    except ValueError as e:
        logger.error("skipping verdict.recorded for case %s: %s", case_id, e)
        return False
    inserted = await service.record(app_id, alert_key, event)
    logger.info(
        "recorded verdict app_id=%s alert_key=%s case_id=%s verdict=%s%s",
        app_id, alert_key, case_id, body["verdict"], "" if inserted else " (duplicate)",
    )
    return True
