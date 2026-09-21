from fastapi import FastAPI
from pydantic import BaseModel

from observability import setup_observability

SERVICE_NAME = "ingestion"

setup_observability(SERVICE_NAME)

app = FastAPI(title=SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)
