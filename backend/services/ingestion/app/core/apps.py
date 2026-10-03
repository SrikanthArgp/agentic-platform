"""Per-app event rules: the payload schema and `alert_key_fields`.

Day 4 interim: read from `backend/apps/{app_id}/manifest.yaml` and the
schema file it names (`event_schema_ref`). Day 5 reads the manifest from
`registry` (30s TTL cache) instead; the schema stays a file in this image
(docs/ARCHITECTURE.md §12). Nothing app-specific is in this code.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

_APP_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class UnknownAppError(LookupError):
    pass


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


class FileAppStore:
    def __init__(self, apps_dir: Path):
        self._apps_dir = apps_dir.resolve()
        self._cache: dict[str, AppEventSpec] = {}

    def get(self, app_id: str) -> AppEventSpec:
        if spec := self._cache.get(app_id):
            return spec
        if not _APP_ID_RE.match(app_id):
            raise UnknownAppError(f"Invalid app_id {app_id!r}.")
        app_dir = self._apps_dir / app_id
        manifest_path = app_dir / "manifest.yaml"
        if not manifest_path.is_file():
            raise UnknownAppError(f"No app '{app_id}'.")
        spec = self._cache[app_id] = _load_spec(app_id, app_dir, manifest_path)
        return spec


def _load_spec(app_id: str, app_dir: Path, manifest_path: Path) -> AppEventSpec:
    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    fields = manifest.get("alert_key_fields")
    schema_ref = manifest.get("event_schema_ref")
    if not fields or not all(isinstance(f, str) and f for f in fields):
        raise AppConfigError(f"{manifest_path}: alert_key_fields must be a non-empty list of field names.")
    if not isinstance(schema_ref, str):
        raise AppConfigError(f"{manifest_path}: event_schema_ref is missing.")
    schema_path = (app_dir / schema_ref).resolve()
    if not schema_path.is_relative_to(app_dir) or not schema_path.is_file():
        raise AppConfigError(f"{manifest_path}: event_schema_ref '{schema_ref}' is not a file in the app's folder.")
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    return AppEventSpec(app_id=app_id, alert_key_fields=tuple(fields), validator=Draft202012Validator(schema))
