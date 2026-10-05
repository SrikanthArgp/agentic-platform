"""Tool registrations: `PUT`/`GET`/`PATCH`/`DELETE /tools/{tool_id}/versions/{version}`.

`register_app.py` registers an app's tools (as served by `tool-gateway`)
before its manifest. `PATCH ... {"enabled": false}` is the runtime switch:
the tool stops being offered to, and callable by, every agent within one
manifest-cache TTL, with no redeploy (docs/plan.md Day 5).
"""

from fastapi import APIRouter, HTTPException, Path, Request, Response, status

from app.core.models import TOOL_ID, VERSION, Tool, ToolEnabledIn, ToolIn, UpsertResult
from app.db.repository import Repository

router = APIRouter(prefix="/tools", tags=["tools"])

ToolIdPath = Path(pattern=TOOL_ID)
VersionPath = Path(pattern=VERSION)


def _repo(request: Request) -> Repository:
    return request.app.state.repo


def _invalid(field: str, message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        {"message": "tool is invalid", "errors": [{"field": field, "message": message}]},
    )


@router.put("/{tool_id}/versions/{version}", response_model=UpsertResult)
async def put_tool(
    body: ToolIn, request: Request, response: Response, tool_id: str = ToolIdPath, version: str = VersionPath
) -> UpsertResult:
    if not body.read_only:
        raise _invalid("read_only", "must be true: every tool is a read-only lookup (ADR-0004).")
    if (body.scope == "app") != (body.app_id is not None):
        raise _invalid("app_id", "is required for scope 'app' and must be absent for scope 'global'.")
    result = await _repo(request).upsert_tool(Tool(tool_id=tool_id, version=version, **body.model_dump()))
    if result.created:
        response.status_code = status.HTTP_201_CREATED
    return result


@router.get("", response_model=list[Tool])
async def list_tools(request: Request) -> list[Tool]:
    return await _repo(request).list_tools()


@router.get("/{tool_id}/versions/{version}", response_model=Tool)
async def get_tool(request: Request, tool_id: str = ToolIdPath, version: str = VersionPath) -> Tool:
    if tool := await _repo(request).get_tool(tool_id, version):
        return tool
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"No tool '{tool_id}' version {version}.")


@router.patch("/{tool_id}/versions/{version}", response_model=Tool)
async def patch_tool(
    body: ToolEnabledIn, request: Request, tool_id: str = ToolIdPath, version: str = VersionPath
) -> Tool:
    if tool := await _repo(request).set_tool_enabled(tool_id, version, body.enabled):
        return tool
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"No tool '{tool_id}' version {version}.")


@router.delete("/{tool_id}/versions/{version}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tool(request: Request, tool_id: str = ToolIdPath, version: str = VersionPath) -> None:
    repo = _repo(request)
    using = [
        a.app_id for a in await repo.list_apps()
        if any((t["tool_id"], t["version"]) == (tool_id, version) for t in a.manifest["tools"])
    ]
    if using:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Tool '{tool_id}' version {version} is declared by apps {using}; disable it instead."
        )
    if not await repo.delete_tool(tool_id, version):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No tool '{tool_id}' version {version}.")
