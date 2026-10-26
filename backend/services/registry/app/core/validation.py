"""App Manifest cross-checks, run on every registration (docs/ARCHITECTURE.md §12).

Shape errors are pydantic's job (`models.py`); this checks what a single
field can't: exactly one entry agent, allowlists against declared tools,
declared tools against registered ones, `invoke_on` values, prefetched
tools against the allowlist and their schema, and `escalate_when` field
paths. Every problem is reported, each naming its
field, so one round trip shows everything wrong with a manifest.

Not checked, because the files live in other services' images:
`prompt_ref` (orchestrator), `event_schema_ref` and whether
`alert_key_fields` exist in that schema (ingestion). Those fail explicitly
for that app's events at resolve time.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from google.protobuf.descriptor import Descriptor, FieldDescriptor
from jsonschema import Draft202012Validator

from app.core.models import ManifestIn, Tool
from app.core.versions import LATEST
from proto_gen import agent_pb2, memory_store_pb2

# Every Decision except the proto3 zero value.
DECISIONS = frozenset(n for n in agent_pb2.Decision.keys() if n != "DECISION_UNSPECIFIED")
_PAYLOAD_PATH = re.compile(r"^payload(\.[A-Za-z0-9_-]+)+$")
# The RunAgentRequest envelope fields a rule may read (ADR-0010); orchestrator's
# guardrails.ENVELOPE_FIELDS must list the same.
ENVELOPE_FIELDS = ("source", "severity", "message", "alert_key")


@dataclass(frozen=True)
class FieldError:
    field: str
    message: str


# (tool_id, version or "latest") -> the registered tool it resolves to.
ToolLookup = Callable[[str, str], Tool | None]


def validate_manifest(manifest: ManifestIn, url_app_id: str, lookup_tool: ToolLookup) -> list[FieldError]:
    errors: list[FieldError] = []

    if manifest.app_id != url_app_id:
        errors.append(FieldError("app_id", f"'{manifest.app_id}' does not match the URL's app_id '{url_app_id}'."))

    errors += _check_tools(manifest, lookup_tool)
    errors += _check_agents(manifest)
    errors += _check_prefetch(manifest, lookup_tool)

    if not manifest.alert_key_fields:
        errors.append(FieldError("alert_key_fields", "must list at least one payload field."))
    elif len(set(manifest.alert_key_fields)) != len(manifest.alert_key_fields):
        errors.append(FieldError("alert_key_fields", "must not repeat a field."))
    for i, name in enumerate(manifest.alert_key_fields):
        if not name:
            errors.append(FieldError(f"alert_key_fields[{i}]", "must not be empty."))

    for i, rule in enumerate(manifest.escalate_when):
        if problem := _escalate_field_problem(rule.field):
            errors.append(FieldError(f"escalate_when[{i}].field", problem))

    return errors


def _check_tools(manifest: ManifestIn, lookup_tool: ToolLookup) -> list[FieldError]:
    errors = []
    seen: set[str] = set()
    for i, ref in enumerate(manifest.tools):
        where = f"tools[{i}]"
        if ref.tool_id in seen:
            errors.append(FieldError(f"{where}.tool_id", f"'{ref.tool_id}' is declared twice."))
            continue
        seen.add(ref.tool_id)
        tool = lookup_tool(ref.tool_id, ref.version)
        if tool is None:
            what = "has no registered version" if ref.version == LATEST else f"version {ref.version} is not registered"
            errors.append(FieldError(where, f"tool '{ref.tool_id}' {what} in registry."))
            continue
        if tool.scope != ref.scope:
            errors.append(
                FieldError(f"{where}.scope", f"'{ref.tool_id}' is registered with scope '{tool.scope}', not '{ref.scope}'.")
            )
        elif tool.scope == "app" and tool.app_id != manifest.app_id:
            # An app may only declare its own app-scoped tools (§3 isolation).
            errors.append(FieldError(where, f"'{ref.tool_id}' belongs to app '{tool.app_id}'."))
    return errors


def _check_agents(manifest: ManifestIn) -> list[FieldError]:
    errors = []
    entries = [a.agent_id for a in manifest.agents if a.role == "entry"]
    if len(entries) != 1:
        errors.append(FieldError("agents", f"must have exactly one agent with role 'entry'; found {len(entries)}."))

    declared = {t.tool_id for t in manifest.tools}
    seen: set[str] = set()
    for i, agent in enumerate(manifest.agents):
        where = f"agents[{i}]"
        if agent.agent_id in seen:
            errors.append(FieldError(f"{where}.agent_id", f"'{agent.agent_id}' is declared twice."))
        seen.add(agent.agent_id)

        for j, tool_id in enumerate(agent.tool_allowlist):
            if tool_id not in declared:
                errors.append(
                    FieldError(f"{where}.tool_allowlist[{j}]", f"'{tool_id}' is not declared in the manifest's tools.")
                )

        if agent.role == "entry" and agent.invoke_on:
            errors.append(FieldError(f"{where}.invoke_on", "must be empty on the entry agent."))
        if agent.role == "callable" and not agent.invoke_on:
            errors.append(FieldError(f"{where}.invoke_on", "a callable agent must list at least one decision."))
        for j, decision in enumerate(agent.invoke_on):
            if decision not in DECISIONS:
                errors.append(
                    FieldError(f"{where}.invoke_on[{j}]", f"'{decision}' is not one of {sorted(DECISIONS)}.")
                )
    return errors


def _check_prefetch(manifest: ManifestIn, lookup_tool: ToolLookup) -> list[FieldError]:
    """Each prefetched tool is on that agent's allowlist and callable with no
    arguments, since orchestrator calls it with `{}` (ADR-0028)."""
    errors = []
    versions = {t.tool_id: t.version for t in manifest.tools}
    for i, agent in enumerate(manifest.agents):
        for j, tool_id in enumerate(agent.prefetch_tools):
            where = f"agents[{i}].prefetch_tools[{j}]"
            if tool_id not in agent.tool_allowlist:
                errors.append(FieldError(where, f"'{tool_id}' is not in this agent's tool_allowlist."))
                continue
            tool = lookup_tool(tool_id, versions.get(tool_id, ""))
            if tool is None:
                continue  # already reported by _check_tools / _check_agents
            if not Draft202012Validator(tool.input_schema).is_valid({}):
                errors.append(FieldError(where, f"'{tool_id}' can't be called with no arguments."))
        if len(set(agent.prefetch_tools)) != len(agent.prefetch_tools):
            errors.append(FieldError(f"agents[{i}].prefetch_tools", "must not repeat a tool."))
    return errors


def _escalate_field_problem(field: str) -> str | None:
    if field.startswith("alert."):
        name = field.removeprefix("alert.")
        return None if name in ENVELOPE_FIELDS else (
            f"'{field}' is not an envelope field; use one of {[f'alert.{n}' for n in ENVELOPE_FIELDS]}."
        )
    if field.startswith("payload."):
        return None if _PAYLOAD_PATH.match(field) else f"'{field}' is not a valid payload.<path>."
    if field.startswith("context."):
        return _context_path_problem(field.removeprefix("context."))
    return f"'{field}' must start with 'alert.', 'payload.' or 'context.'."


def _context_path_problem(path: str) -> str | None:
    """`path` must name a scalar field of `GetContextResponse`, e.g.
    `has_confirmed_incident_history` or `window_24h.escalation_count`."""
    descriptor: Descriptor | None = memory_store_pb2.GetContextResponse.DESCRIPTOR
    parts = path.split(".")
    for i, part in enumerate(parts):
        if descriptor is None or part not in descriptor.fields_by_name:
            return f"'context.{path}' is not a field of GetContextResponse."
        field = descriptor.fields_by_name[part]
        is_message = field.type == FieldDescriptor.TYPE_MESSAGE
        if i == len(parts) - 1 and is_message:
            return f"'context.{path}' is a message; name one of its fields."
        descriptor = field.message_type if is_message else None
    return None
