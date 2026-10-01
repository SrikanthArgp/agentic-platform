# ADR-0001: Protocol per purpose — REST, gRPC, MCP, Kafka

- **Status**: Accepted
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §9

## Context

Six services talk to each other with very different needs: an external
client hands off an alert and shouldn't wait for an LLM; `orchestrator`
needs memory context *before* it can reason, in milliseconds; agents call
tools through a model-native tool interface; humans read and write cases.
One uniform transport would fit some of these badly.

## Decision

Pick the transport per link by what the link needs:

| Need | Transport | Links |
|---|---|---|
| Async hand-off, buffering, replay | Kafka | `alert.received`, `alert.decided`, `verdict.recorded` |
| Synchronous, typed, low-latency internal call | gRPC | `orchestrator → memory-store` (`GetContext`), `RunAgent` entrypoint |
| LLM tool calling | MCP | `orchestrator → tool-gateway` |
| External/human-facing and simple reads | REST | alert intake, analyst API, `registry` reads, `similar-past-case-lookup` |

One protobuf contract (`RunAgentRequest`/`Response`) is shared by the gRPC
entrypoint and the Kafka payloads, so there's one schema, not two.

## Alternatives considered

- **REST everywhere** — simplest to operate, but the alert path would be
  synchronous (client waits on an LLM) or need a hand-rolled queue, and
  `GetContext` loses typed contracts and gets slower.
- **gRPC everywhere** — typed and fast, but no buffering or replay for the
  bursty alert path, and awkward for external clients and humans.
- **Kafka everywhere** — request/response over Kafka (`GetContext`) adds
  latency and correlation plumbing for no benefit.

## Consequences

- ✅ Each link uses the transport whose failure and latency behavior fits it.
- ✅ One shared contract across gRPC and Kafka avoids schema drift.
- ❌ Four protocols to learn, instrument (trace propagation in each), and
  secure (§13 T12) instead of one.
- ❌ More moving parts in local development.

Revisit if operating four protocols costs more than the fit is worth —
most likely by collapsing gRPC into REST if `GetContext` latency turns out
not to matter.
