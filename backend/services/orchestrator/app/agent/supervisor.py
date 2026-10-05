"""The supervisor step: low confidence -> ESCALATE (docs/adr/0020).

Code, not an agent or a tool: it runs after the agent loop, before the
guardrails, and can only move a decision to ESCALATE.

Confidence is the agent's own estimate, capped by rules `orchestrator`
checks itself, so nothing in the payload or the LLM's answer can raise it
past them:

| condition                                                  | cap |
|------------------------------------------------------------|-----|
| memory context unavailable                                  | 0.5 |
| `is_novel_alert` and the decision isn't ESCALATE           | 0.5 |
| `has_confirmed_incident_history` and the decision is SUPPRESS | 0.3 |
"""

from dataclasses import dataclass

from proto_gen import agent_pb2, memory_store_pb2

NO_CONTEXT_CAP = 0.5
NOVEL_ALERT_CAP = 0.5
CONFIRMED_INCIDENT_SUPPRESS_CAP = 0.3


@dataclass(frozen=True)
class Supervised:
    decision: agent_pb2.Decision
    confidence: float
    # Empty unless the supervisor changed the decision.
    reason: str | None = None


def caps(
    decision: agent_pb2.Decision, context: memory_store_pb2.GetContextResponse | None
) -> list[tuple[float, str]]:
    """(cap, why) for every rule that applies to this decision."""
    out = []
    if context is None:
        out.append((NO_CONTEXT_CAP, "no memory context"))
    else:
        if context.is_novel_alert and decision != agent_pb2.ESCALATE:
            out.append((NOVEL_ALERT_CAP, "first time this alert_key is seen"))
        if context.has_confirmed_incident_history and decision == agent_pb2.SUPPRESS:
            out.append((CONFIRMED_INCIDENT_SUPPRESS_CAP, "an analyst confirmed a real incident on this alert_key"))
    return out


def supervise(
    decision: agent_pb2.Decision,
    agent_confidence: float,
    context: memory_store_pb2.GetContextResponse | None,
    min_confidence: float,
) -> Supervised:
    applied = caps(decision, context)
    confidence = min([agent_confidence, *(cap for cap, _ in applied)])
    if decision == agent_pb2.ESCALATE or confidence >= min_confidence:
        return Supervised(decision, confidence)

    capped = [why for cap, why in applied if cap == confidence and cap < agent_confidence]
    source = f"capped at {confidence:.2f}: {'; '.join(capped)}" if capped else "the agent's own estimate"
    return Supervised(
        agent_pb2.ESCALATE,
        confidence,
        f"supervisor: confidence {confidence:.2f} ({source}) is below this app's threshold "
        f"{min_confidence:.2f}; {agent_pb2.Decision.Name(decision)} changed to ESCALATE for human review.",
    )
