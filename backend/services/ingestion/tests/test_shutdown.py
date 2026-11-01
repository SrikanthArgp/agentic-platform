"""SIGTERM against a real uvicorn server (docs/plan.md Day 19): readiness
flips at once, and a request already in flight still gets its `202`."""

import asyncio
import signal
import socket

import httpx
import pytest
import uvicorn

from app.core.config import Settings
from app.main import create_app
from tests.conftest import EVENTS_URL, FakeOutbox, FakeRegistry, alert_body

pytestmark = pytest.mark.anyio


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def harmless_sigterm():
    # uvicorn re-raises the signal it caught once it has exited; make that a no-op.
    original = signal.signal(signal.SIGTERM, lambda *_: None)
    yield
    signal.signal(signal.SIGTERM, original)


async def test_sigterm_flips_readiness_and_finishes_the_in_flight_request(apps_dir, harmless_sigterm):
    outbox = FakeOutbox(write_delay_s=0.5)
    app = create_app(Settings(apps_dir=apps_dir), outbox=outbox, apps=FakeRegistry())
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
        assert (await http.get("/readyz")).status_code == 200
        in_flight = asyncio.create_task(http.post(EVENTS_URL, json=alert_body()))
        await asyncio.sleep(0.1)  # the write has started

        signal.raise_signal(signal.SIGTERM)
        assert app.state.lifecycle.draining is True

        response = await in_flight
    await asyncio.wait_for(serving, 5)

    assert response.status_code == 202
    assert [row.alert_id for row in outbox.rows] == [response.json()["alert_id"]]
