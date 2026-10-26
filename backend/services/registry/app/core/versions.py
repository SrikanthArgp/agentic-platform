"""Tool version references in a manifest: a pinned `MAJOR.MINOR.PATCH`, or
`latest` (docs/plan.md Day 13, ADR-0026).

`latest` is the highest registered version by number (`1.10.0` > `1.9.0`),
whether or not it's enabled: disabling it turns the tool off, it never
falls back to an older version. It is resolved on every `GET /apps/{app_id}`,
so registering a new version moves every `latest` manifest onto it within
one manifest-cache TTL, with no re-registration.
"""

from collections.abc import Iterable

from app.core.models import Tool

LATEST = "latest"


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def newest(tools: Iterable[Tool]) -> Tool | None:
    return max(tools, key=lambda t: version_key(t.version), default=None)


def resolve_ref(requested: str, versions: Iterable[Tool]) -> Tool | None:
    """The registered tool a manifest's `version` refers to, or None."""
    versions = list(versions)
    if requested == LATEST:
        return newest(versions)
    return next((t for t in versions if t.version == requested), None)
