"""Startup scan of `backend/apps/*/tools/` (docs/ARCHITECTURE.md §12).

App directories use the hyphenated `app_id` (`it-ops-triage`), which isn't a
valid Python package name, so each tool module is imported by file path. A
module is any `*.py` directly under `tools/` not starting with `_`; it must
export `TOOLS: dict[tool_id, dict]` with exactly the keys in
`_REQUIRED_KEYS`. App modules never import `tool-gateway` code: that dict is
the whole contract.

Any problem (import error, bad `TOOLS`, duplicate `tool_id`) fails startup
with a `ToolLoadError` naming the file. A tool that silently fails to load
would only show up later as a `tool_not_found` during a live run.
"""

import importlib.util
import logging
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel

from app.core.registry import DuplicateToolError, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

_REQUIRED_KEYS = {"version", "description", "input_model", "output_model", "handler"}
# The module namespace app tool modules are imported under; never a real package.
_MODULE_NAMESPACE = "_apptools"


class ToolLoadError(RuntimeError):
    pass


def load_app_tools(apps_dir: Path, registry: ToolRegistry) -> None:
    """Register every app-scoped tool found under `apps_dir/*/tools/`."""
    if not apps_dir.is_dir():
        raise ToolLoadError(f"apps directory does not exist: {apps_dir}")

    for app_dir in sorted(p for p in apps_dir.iterdir() if p.is_dir()):
        tools_dir = app_dir / "tools"
        if not tools_dir.is_dir():
            continue
        app_id = app_dir.name
        for module_path in sorted(tools_dir.glob("*.py")):
            if module_path.name.startswith("_"):
                continue
            module = _import_by_path(app_id, module_path)
            for spec in _tool_specs(app_id, module_path, module):
                try:
                    registry.add(spec)
                except DuplicateToolError as e:
                    raise ToolLoadError(f"{module_path}: {e}") from e
                logger.info("registered tool %s v%s (app_id=%s)", spec.tool_id, spec.version, app_id)


def _import_by_path(app_id: str, module_path: Path) -> ModuleType:
    safe_app = re.sub(r"\W", "_", app_id)
    module_name = f"{_MODULE_NAMESPACE}.{safe_app}.{module_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ToolLoadError(f"{module_path}: cannot be imported")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so pydantic can resolve the module's own forward refs.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        del sys.modules[module_name]
        raise ToolLoadError(f"{module_path}: import failed: {e!r}") from e
    return module


def _tool_specs(app_id: str, module_path: Path, module: ModuleType) -> list[ToolSpec]:
    tools = getattr(module, "TOOLS", None)
    if not isinstance(tools, dict):
        raise ToolLoadError(f"{module_path}: must export TOOLS as a dict of tool_id -> tool definition")
    return [_tool_spec(app_id, module_path, tool_id, d) for tool_id, d in tools.items()]


def _tool_spec(app_id: str, module_path: Path, tool_id: Any, definition: Any) -> ToolSpec:
    where = f"{module_path}: tool '{tool_id}'"
    if not isinstance(tool_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", tool_id):
        raise ToolLoadError(f"{where}: tool_id must match [a-z][a-z0-9_-]{{0,63}}")
    if not isinstance(definition, dict):
        raise ToolLoadError(f"{where}: definition must be a dict")
    missing = _REQUIRED_KEYS - definition.keys()
    unknown = definition.keys() - _REQUIRED_KEYS
    if missing or unknown:
        raise ToolLoadError(f"{where}: missing keys {sorted(missing)}, unknown keys {sorted(unknown)}")
    for key in ("input_model", "output_model"):
        model = definition[key]
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise ToolLoadError(f"{where}: {key} must be a pydantic BaseModel subclass")
    if not callable(definition["handler"]):
        raise ToolLoadError(f"{where}: handler must be callable")
    for key in ("version", "description"):
        if not isinstance(definition[key], str) or not definition[key]:
            raise ToolLoadError(f"{where}: {key} must be a non-empty string")

    return ToolSpec(
        tool_id=tool_id,
        version=definition["version"],
        description=definition["description"],
        scope="app",
        app_id=app_id,
        input_model=definition["input_model"],
        output_model=definition["output_model"],
        handler=definition["handler"],
    )
