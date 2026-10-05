"""App Manifests: `PUT`/`GET`/`DELETE /apps/{app_id}`, `GET /apps`, `GET /agents`.

`PUT` is what `register_app.py` calls (ADR-0006): validated in full, then
upserted; an unchanged manifest reports `changed: false`. `GET` serves the
manifest resolved against current tool state (`resolve.py`), which is what
`orchestrator`, `ingestion` and `tool-gateway` cache for 30s.

Agents are registered as part of their app's manifest, not on their own:
an agent only exists within one app (§3). `GET /agents` lists them.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from pydantic import BaseModel

from app.core.models import SLUG, AppRecord, ManifestIn, ResolvedApp, Tool, UpsertResult
from app.core.resolve import resolve_app
from app.core.validation import validate_manifest
from app.db.repository import Repository

router = APIRouter(tags=["apps"])

AppIdPath = Path(pattern=SLUG)


class AppSummary(BaseModel):
    app_id: str
    display_name: str
    updated_at: datetime | None = None


class AgentSummary(BaseModel):
    app_id: str
    agent_id: str
    version: str
    role: str


def _repo(request: Request) -> Repository:
    return request.app.state.repo


async def _declared_tools(repo: Repository, manifest: dict) -> dict[tuple[str, str], Tool]:
    found = {}
    for ref in manifest["tools"]:
        key = (ref["tool_id"], ref["version"])
        if tool := await repo.get_tool(*key):
            found[key] = tool
    return found


@router.put("/apps/{app_id}", response_model=UpsertResult)
async def put_app(
    body: ManifestIn, request: Request, response: Response, app_id: str = AppIdPath
) -> UpsertResult:
    repo = _repo(request)
    stored = body.stored()
    tools = await _declared_tools(repo, stored)
    if errors := validate_manifest(body, app_id, lambda tool_id, version: tools.get((tool_id, version))):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"message": "manifest is invalid", "errors": [{"field": e.field, "message": e.message} for e in errors]},
        )
    result = await repo.upsert_app(app_id, body.display_name, stored)
    if result.created:
        response.status_code = status.HTTP_201_CREATED
    return result


@router.get("/apps", response_model=list[AppSummary])
async def list_apps(request: Request) -> list[AppSummary]:
    return [AppSummary(app_id=a.app_id, display_name=a.display_name, updated_at=a.updated_at)
            for a in await _repo(request).list_apps()]


@router.get("/apps/{app_id}", response_model=ResolvedApp)
async def get_app(request: Request, app_id: str = AppIdPath) -> ResolvedApp:
    repo = _repo(request)
    record = await _get_record(repo, app_id)
    return resolve_app(record, await _declared_tools(repo, record.manifest))


@router.delete("/apps/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_app(request: Request, app_id: str = AppIdPath) -> None:
    if not await _repo(request).delete_app(app_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No app '{app_id}' is registered.")


@router.get("/agents", response_model=list[AgentSummary])
async def list_agents(request: Request) -> list[AgentSummary]:
    return [
        AgentSummary(app_id=a.app_id, agent_id=g["agent_id"], version=g["version"], role=g["role"])
        for a in await _repo(request).list_apps()
        for g in a.manifest["agents"]
    ]


async def _get_record(repo: Repository, app_id: str) -> AppRecord:
    if record := await repo.get_app(app_id):
        return record
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"No app '{app_id}' is registered.")
