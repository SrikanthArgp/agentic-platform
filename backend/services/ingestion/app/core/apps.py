"""Per-app event rules: the payload schema and `alert_key_fields`.

The manifest comes from `registry` (`ap-shared`'s `RegistryClient`, the
platform's 30s TTL cache, docs/ARCHITECTURE.md §3); the schema it names
(`event_schema_ref`) is a file in this image (§12). Nothing app-specific is
in this code.

`registry` can't see that file, so two checks happen here, the first time a
manifest version is resolved: the schema exists and is valid, and every
`alert_key_fields` entry is a property it declares. Either failing is a
platform problem (`AppConfigError`) that fails that app's events explicitly.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from registry_client import AppNotFoundError, RegistryUnavailableError


class UnknownAppError(LookupError):
    pass


class AppUnavailableError(RuntimeError):
    """`registry` is unreachable and there's no cached copy of the app."""


class AppConfigError(ValueError):
    """The app's manifest or schema is broken: a platform problem, not the caller's."""


@dataclass(frozen=True)
class AppEventSpec:
    app_id: str
    alert_key_fields: tuple[str, ...]
    validator: Draft202012Validator

    def payload_errors(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        """Schema violations as `{path, message}`, in a stable order; empty if valid."""
        errors = sorted(self.validator.iter_errors(payload), key=lambda e: list(map(str, e.absolute_path)))
        return [
            {"path": "payload" + "".join(f".{p}" for p in e.absolute_path), "message": e.message} for e in errors
        ]


class AppSource(Protocol):
    """`registry_client.RegistryClient`, or a fake in tests."""

    async def get_app(self, app_id: str) -> dict[str, Any]: ...


class AppStore:
    def __init__(self, registry: AppSource, apps_dir: Path):
        self._registry = registry
        self._apps_dir = apps_dir.resolve()
        # Keyed by everything the spec is built from, so a manifest change
        # (new schema ref or key fields) builds a new one on the next fetch.
        self._specs: dict[tuple[str, str, tuple[str, ...]], AppEventSpec] = {}

    async def get(self, app_id: str) -> AppEventSpec:
        try:
            app = await self._registry.get_app(app_id)
        except AppNotFoundError:
            raise UnknownAppError(f"No app '{app_id}' is registered.") from None
        except RegistryUnavailableError as e:
            raise AppUnavailableError(str(e)) from None

        schema_ref = app.get("event_schema_ref")
        fields = app.get("alert_key_fields")
        if not isinstance(schema_ref, str) or not fields or not all(isinstance(f, str) and f for f in fields):
            raise AppConfigError(f"App '{app_id}': manifest lacks event_schema_ref or alert_key_fields.")
        key = (app_id, schema_ref, tuple(fields))
        if spec := self._specs.get(key):
            return spec
        spec = self._specs[key] = self._load_spec(app_id, schema_ref, tuple(fields))
        return spec

    def _load_spec(self, app_id: str, schema_ref: str, fields: tuple[str, ...]) -> AppEventSpec:
        app_dir = self._apps_dir / app_id
        schema_path = (app_dir / schema_ref).resolve()
        if not schema_path.is_relative_to(app_dir) or not schema_path.is_file():
            raise AppConfigError(f"App '{app_id}': event_schema_ref '{schema_ref}' is not a file in the app's folder.")
        schema = json.loads(schema_path.read_text())
        Draft202012Validator.check_schema(schema)
        declared = schema.get("properties", {})
        if missing := [f for f in fields if f not in declared]:
            raise AppConfigError(f"App '{app_id}': alert_key_fields {missing} are not properties of {schema_ref}.")
        return AppEventSpec(app_id=app_id, alert_key_fields=fields, validator=Draft202012Validator(schema))
