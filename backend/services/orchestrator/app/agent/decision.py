"""Parse an agent's final answer: an entry agent's decision, confidence and
reasons (`parse_decision`), or a callable agent's reasons only
(`parse_explanation`, Day 8).

Anything that doesn't parse cleanly is an error for the caller to turn into
ESCALATE (fail toward escalation, docs/ARCHITECTURE.md §13.4); this module
never guesses a decision.
"""

import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from proto_gen import agent_pb2

MAX_REASONS = 10
MAX_REASON_CHARS = 500

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class DecisionParseError(ValueError):
    pass


class _Explanation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reasons: list[str] = Field(min_length=1)


class _FinalAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision: Literal["AUTO_RESOLVE", "ESCALATE", "SUPPRESS"]
    # The agent's own estimate, 0-1; the supervisor caps it (docs/adr/0020).
    # strict: `true` is not a confidence.
    confidence: float = Field(ge=0, le=1, strict=True)
    reasons: list[str] = Field(min_length=1)


@dataclass(frozen=True)
class ParsedDecision:
    decision: agent_pb2.Decision
    confidence: float
    reasons: list[str]


def parse_decision(text: str | None) -> ParsedDecision:
    answer = _parse(text, _FinalAnswer)
    reasons = _clean(answer.reasons)
    return ParsedDecision(
        decision=agent_pb2.Decision.Value(answer.decision), confidence=answer.confidence, reasons=reasons
    )


def parse_explanation(text: str | None) -> list[str]:
    """A callable agent's `{"reasons": [...]}`, cleaned like a decision's."""
    return _clean(_parse(text, _Explanation).reasons)


def _parse(text: str | None, model: type[BaseModel]) -> BaseModel:
    if not text or not text.strip():
        raise DecisionParseError("Agent gave no final answer.")
    body = text.strip()
    if fenced := _FENCE_RE.match(body):
        body = fenced.group(1)
    try:
        return model.model_validate(json.loads(body))
    except json.JSONDecodeError as e:
        raise DecisionParseError("Agent's final answer is not JSON.") from e
    except ValidationError as e:
        fields = ", ".join(".".join(map(str, err["loc"])) or "(root)" for err in e.errors())
        raise DecisionParseError(f"Agent's final answer is invalid ({fields}).") from e


def _clean(reasons: list[str]) -> list[str]:
    cleaned = [r.strip()[:MAX_REASON_CHARS] for r in reasons if r.strip()][:MAX_REASONS]
    if not cleaned:
        raise DecisionParseError("Agent's final answer has no non-empty reasons.")
    return cleaned
