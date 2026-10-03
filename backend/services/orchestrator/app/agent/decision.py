"""Parse the agent's final answer into a decision and reasons.

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


class _FinalAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision: Literal["AUTO_RESOLVE", "ESCALATE", "SUPPRESS"]
    reasons: list[str] = Field(min_length=1)


@dataclass(frozen=True)
class ParsedDecision:
    decision: agent_pb2.Decision
    reasons: list[str]


def parse_decision(text: str | None) -> ParsedDecision:
    if not text or not text.strip():
        raise DecisionParseError("Agent gave no final answer.")
    body = text.strip()
    if fenced := _FENCE_RE.match(body):
        body = fenced.group(1)
    try:
        answer = _FinalAnswer.model_validate(json.loads(body))
    except json.JSONDecodeError as e:
        raise DecisionParseError("Agent's final answer is not JSON.") from e
    except ValidationError as e:
        fields = ", ".join(".".join(map(str, err["loc"])) or "(root)" for err in e.errors())
        raise DecisionParseError(f"Agent's final answer is invalid ({fields}).") from e

    reasons = [r.strip()[:MAX_REASON_CHARS] for r in answer.reasons if r.strip()][:MAX_REASONS]
    if not reasons:
        raise DecisionParseError("Agent's final answer has no non-empty reasons.")
    return ParsedDecision(decision=agent_pb2.Decision.Value(answer.decision), reasons=reasons)
