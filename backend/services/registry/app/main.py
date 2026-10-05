"""registry: tool registrations and App Manifests (docs/ARCHITECTURE.md §3, §12)."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.api.apps import router as apps_router
from app.api.tools import router as tools_router
from app.db.repository import Repository
from observability import setup_observability

SERVICE_NAME = "registry"
DEFAULT_POSTGRES_DSN = "postgresql://platform:platform@localhost:5432/platform"

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


def create_app(repo: Repository | None = None) -> FastAPI:
    """`repo` is injected in tests; otherwise a Postgres pool is opened with the app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if repo is not None:
            yield
            return
        from app.db.postgres import PostgresRepository, create_pool

        pool = await create_pool(os.environ.get("POSTGRES_DSN", DEFAULT_POSTGRES_DSN))
        app.state.repo = PostgresRepository(pool)
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.state.repo = repo
    app.include_router(tools_router)
    app.include_router(apps_router)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", service=SERVICE_NAME)

    return app


app = create_app()
