"""RegistryClient: TTL cache, negative cache, stale-on-error."""

import httpx
import pytest

from registry_client import AppNotFoundError, RegistryClient, RegistryUnavailableError

pytestmark = pytest.mark.anyio

APP = {"app_id": "it-ops-triage", "agents": []}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeRegistry:
    def __init__(self):
        self.calls = 0
        self.status = 200
        self.body = APP
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(self.status, json=self.body)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def setup():
    registry, clock = FakeRegistry(), Clock()
    client = RegistryClient("http://registry", ttl_s=30, transport=httpx.MockTransport(registry.handle), clock=clock)
    return registry, clock, client


async def test_fetches_once_per_ttl(setup):
    registry, clock, client = setup
    assert await client.get_app("it-ops-triage") == APP
    clock.now += 29
    await client.get_app("it-ops-triage")
    assert registry.calls == 1

    registry.body = {**APP, "display_name": "changed"}
    clock.now += 1
    assert (await client.get_app("it-ops-triage"))["display_name"] == "changed"
    assert registry.calls == 2


async def test_unknown_app_is_cached_too(setup):
    registry, clock, client = setup
    registry.status = 404
    for _ in range(3):
        with pytest.raises(AppNotFoundError):
            await client.get_app("made-up")
    assert registry.calls == 1


async def test_invalid_app_id_never_reaches_registry(setup):
    registry, _, client = setup
    with pytest.raises(AppNotFoundError):
        await client.get_app("../tools")
    assert registry.calls == 0


async def test_unavailable_without_a_cached_copy_raises(setup):
    registry, _, client = setup
    registry.down = True
    with pytest.raises(RegistryUnavailableError):
        await client.get_app("it-ops-triage")


async def test_server_error_is_unavailable(setup):
    registry, _, client = setup
    registry.status = 500
    with pytest.raises(RegistryUnavailableError):
        await client.get_app("it-ops-triage")


async def test_expired_copy_is_served_while_registry_is_down(setup):
    registry, clock, client = setup
    await client.get_app("it-ops-triage")
    registry.down = True
    clock.now += 60
    assert await client.get_app("it-ops-triage") == APP
