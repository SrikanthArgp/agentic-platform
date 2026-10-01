# ADR-0012: Run callable agents in-process instead of a gRPC self-call

- **Status**: Proposed
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §5, §9; `plan.md` Day 8

## Context

The current design runs callable agents (e.g. `root-cause-summarizer`) by
having `orchestrator` call its own `Agent.RunAgent` gRPC service — usually
the same process, going through the network stack.

## Proposal

Keep `RunAgent` as the external/test entrypoint, but dispatch callables
in-process through the same handler function: identical request/response
types and `agent_id` dispatch, no network hop.

## Alternatives considered

- **Keep the gRPC self-call** (current design) — uniform, and would allow
  callables to move to a separate deployment later without code change;
  pays serialization, connection, and failure-mode costs on every
  escalation for a deployment split nobody has planned.

## Consequences if accepted

- ✅ Lower latency, fewer failure modes, simpler tracing for delegation.
- ❌ Moving callables to their own deployment later needs a code change
  (swap the dispatcher), which is small because the contract is unchanged.

Not yet decided — needs a call at the start of Day 8. If accepted, update
`ARCHITECTURE.md` §5/§9 and `plan.md` Day 8.
