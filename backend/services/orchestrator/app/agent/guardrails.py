"""Deterministic `escalate_when` guardrails (ADR-0010).

A rule is `{field, in}`. `field` is one of:

- `alert.<envelope field>`: `source`, `severity`, `message`, `alert_key`;
- `payload.<path>`: a value in the app's event payload, dots walking objects;
- `context.<path>`: a scalar field of the run's `GetContextResponse`,
  e.g. `has_confirmed_incident_history` or `window_24h.escalation_count`.

A rule matches when the field's value equals one of the `in` values. Types
must agree (`true` never equals `1`, `"5"` never equals `5`); numbers
compare by value, since `Struct` stores every number as a double. A missing
field, an object or list value, or a `context.*` rule with no context,
never matches.

Pure functions only: `loop.py` decides what a match does (force ESCALATE,
add a reason naming the rule).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

from proto_gen import agent_pb2, memory_store_pb2

ENVELOPE_FIELDS = ("source", "severity", "message", "alert_key")

Scalar = str | int | float | bool


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


MISSING = _Missing()


@dataclass(frozen=True)
class Rule:
    field: str
    values: Sequence[Scalar]


@dataclass(frozen=True)
class Match:
    index: int
    rule: Rule
    value: Scalar

    def describe(self) -> str:
        """e.g. `escalate_when[0] alert.severity = "critical"`."""
        return f"escalate_when[{self.index}] {self.rule.field} = {_show(self.value)}"


def evaluate(
    rules: Sequence[Rule],
    request: agent_pb2.RunAgentRequest,
    context: memory_store_pb2.GetContextResponse | None,
) -> list[Match]:
    """Every rule that matches, in manifest order."""
    payload = MessageToDict(request.payload) if request.HasField("payload") else {}
    matches = []
    for i, rule in enumerate(rules):
        value = lookup(rule.field, request, payload, context)
        if value is not MISSING and any(equal(value, allowed) for allowed in rule.values):
            matches.append(Match(i, rule, value))
    return matches


def lookup(
    field: str,
    request: agent_pb2.RunAgentRequest,
    payload: dict[str, Any],
    context: memory_store_pb2.GetContextResponse | None,
) -> Scalar | _Missing:
    prefix, _, path = field.partition(".")
    if not path:
        return MISSING
    if prefix == "alert":
        return getattr(request, path) if path in ENVELOPE_FIELDS else MISSING
    if prefix == "payload":
        return _walk_dict(payload, path.split("."))
    if prefix == "context":
        return MISSING if context is None else _walk_message(context, path.split("."))
    return MISSING


def equal(value: Scalar, allowed: Scalar) -> bool:
    if isinstance(value, bool) or isinstance(allowed, bool):
        return isinstance(value, bool) and isinstance(allowed, bool) and value == allowed
    if isinstance(value, int | float) and isinstance(allowed, int | float):
        return value == allowed
    if isinstance(value, str) and isinstance(allowed, str):
        return value == allowed
    return False


def _walk_dict(node: Any, parts: list[str]) -> Scalar | _Missing:
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return node if isinstance(node, Scalar) else MISSING


def _walk_message(node: Message, parts: list[str]) -> Scalar | _Missing:
    value: Any = node
    for part in parts:
        if not isinstance(value, Message) or part not in value.DESCRIPTOR.fields_by_name:
            return MISSING
        value = getattr(value, part)
    return value if isinstance(value, Scalar) else MISSING


def _show(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    return f"{value:g}" if isinstance(value, float) else str(value)
