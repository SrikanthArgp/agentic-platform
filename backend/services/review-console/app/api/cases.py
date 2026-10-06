"""The analyst API: `GET /cases`, `GET /cases/{id}`, `POST /cases/{id}/verdict`.

`GET /cases` always takes `app_id` (every read is app-scoped, §8); it's
also what Day 13's `similar-past-case-lookup` calls with `alert_key`.
Verdict errors: unknown case 404, already resolved 409 (with the existing
verdict), Kafka unreachable 503 (the case stays `OPEN`; retry).
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.core.cases import Case, CaseAlreadyResolvedError, CaseNotFoundError, CaseStatus, VerdictIn
from app.core.service import CaseQuery, CaseService
from app.kafka.publisher import PublishError

logger = logging.getLogger(__name__)

router = APIRouter()


class CaseList(BaseModel):
    cases: list[Case]


def _service(request: Request) -> CaseService:
    return request.app.state.service


@router.get("/cases", response_model=CaseList)
async def list_cases(
    request: Request,
    app_id: str = Query(min_length=1, max_length=63),
    alert_key: str | None = Query(default=None, max_length=512),
    case_status: CaseStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CaseList:
    query = CaseQuery(app_id=app_id, alert_key=alert_key, status=case_status, limit=limit, offset=offset)
    return CaseList(cases=await _service(request).list(query))


@router.get("/cases/{case_id}", response_model=Case)
async def get_case(case_id: int, request: Request) -> Case:
    try:
        return await _service(request).get(case_id)
    except CaseNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"case {case_id} not found") from None


@router.post("/cases/{case_id}/verdict", response_model=Case)
async def post_verdict(case_id: int, verdict: VerdictIn, request: Request) -> Case:
    try:
        case = await _service(request).record_verdict(case_id, verdict)
    except CaseNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"case {case_id} not found") from None
    except CaseAlreadyResolvedError as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"message": str(e), "verdict": e.case.verdict, "verdict_by": e.case.verdict_by},
        ) from None
    except PublishError as e:
        logger.warning("verdict on case %d not recorded: %s", case_id, e)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Verdicts are temporarily unavailable; retry.",
            headers={"Retry-After": "5"},
        ) from None
    logger.info(
        "case %d resolved app_id=%s alert_key=%s verdict=%s by=%s",
        case.id, case.app_id, case.alert_key, case.verdict, case.verdict_by,
    )
    return case
