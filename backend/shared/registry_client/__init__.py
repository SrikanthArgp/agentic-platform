"""`registry` client with the platform's manifest cache (docs/ARCHITECTURE.md §3, §12).

`orchestrator`, `ingestion` and `tool-gateway` all read an app through this,
with the same TTL (default 30s), so they never disagree about a manifest for
longer than one TTL, and a change in `registry` (e.g. a disabled tool)
reaches all of them on their next fetch without a restart.

`get_app()` returns `GET /apps/{app_id}` as a dict; each service parses the
fields it uses. Failures:

- `AppNotFoundError`: no such app (also cached for one TTL, so a flood of
  events for a made-up `app_id` doesn't become a flood of registry calls);
- `RegistryUnavailableError`: registry unreachable or erroring, and there is
  no earlier copy. With an earlier copy (even expired), that copy is served
  and the failure logged: a registry outage shouldn't stop triage of apps
  that were already resolved. The cost is that a change made during the
  outage takes effect only once registry is back.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 30.0
DEFAULT_TIMEOUT_S = 2.0
_APP_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class AppNotFoundError(LookupError):
    pass


class RegistryUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Entry:
    fetched_at: float
    app: dict[str, Any] | None  # None: registry said 404


class RegistryClient:
    def __init__(
        self,
        base_url: str,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """`transport` and `clock` are for tests."""
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport)
        self._ttl_s = ttl_s
        self._clock = clock
        self._cache: dict[str, _Entry] = {}

    async def get_app(self, app_id: str) -> dict[str, Any]:
        if not _APP_ID_RE.match(app_id):
            # Never becomes a URL segment, never cached.
            raise AppNotFoundError(f"Invalid app_id {app_id!r}.")

        entry = self._cache.get(app_id)
        if entry is None or self._clock() - entry.fetched_at >= self._ttl_s:
            try:
                entry = self._cache[app_id] = _Entry(self._clock(), await self._fetch(app_id))
            except RegistryUnavailableError as e:
                if entry is None:
                    raise
                logger.warning("registry unavailable (%s); serving cached app %s", e, app_id)

        if entry.app is None:
            raise AppNotFoundError(f"No app '{app_id}' is registered.")
        return entry.app

    async def _fetch(self, app_id: str) -> dict[str, Any] | None:
        try:
            response = await self._http.get(f"/apps/{app_id}")
        except httpx.HTTPError as e:
            raise RegistryUnavailableError(f"GET /apps/{app_id} failed: {e!r}") from e
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RegistryUnavailableError(f"GET /apps/{app_id} returned {response.status_code}")
        return response.json()

    async def aclose(self) -> None:
        await self._http.aclose()
