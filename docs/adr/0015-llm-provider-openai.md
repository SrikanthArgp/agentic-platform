# ADR-0015: OpenAI as the LLM provider for this build, behind a provider-agnostic interface

- **Status**: Accepted
- **Date**: 2026-10-03
- **See**: `ARCHITECTURE.md` §5, §13 (T1, T2, T10); `plan.md` Days 3, 14, 24; `ENTERPRISE_READINESS.md` §3

## Context

Day 3 needs a real LLM with tool calling for the `orchestrator` agent loop.
The choice has to be cheap enough to run on every simulated alert (Day 21),
in evals (Day 25), and in load tests (Day 26), and good enough at tool use
and following a fixed output format that the triage decisions are worth
evaluating. The project has an OpenAI API key available; it has no
Anthropic API key (a Claude subscription doesn't include API access).

## Decision

- **Provider: OpenAI**, via the official `openai` Python SDK (Chat
  Completions with function tools). The SDK reads `OPENAI_API_KEY` itself;
  Compose passes it to `orchestrator` only, from the gitignored
  `backend/local/.env`.
- **Default model: `gpt-5.4-mini`**: small and cheap, with reliable tool
  calling. Set per deployment with `LLM_MODEL`; nothing in code depends on
  the model name.
- **The agent loop never calls the SDK.** `app/agent/llm.py` defines the
  interface (messages, tool definitions, tool calls, token usage) and
  `app/agent/openai_llm.py` is the only module importing `openai`. Adding a
  provider is a new adapter plus a `LLM_PROVIDER` value, not a change to the
  loop.
- **Final answers are plain JSON** (`{"decision", "reasons"}`) parsed and
  validated by the platform, not a provider feature such as structured
  outputs. Anything that doesn't parse becomes `ESCALATE`.

## Alternatives considered

- **Anthropic (Claude)**: equally capable at tool use; not chosen only
  because no API key is available for this build. The interface keeps it
  a one-adapter change.
- **Local model (Ollama etc.)**: no cost and no data leaving the machine
  (T10), but noticeably weaker, less consistent tool calling, which is the
  thing the decisions depend on. Worth revisiting as a second adapter for
  offline dev.
- **OpenAI Responses API instead of Chat Completions**: richer, but Chat
  Completions' message/tool shape maps one-to-one onto the
  provider-agnostic interface and onto most other providers' APIs.
- **Provider-native structured outputs for the final answer**: stronger
  format guarantees, but ties the decision format to one provider. The
  platform-side parser plus fail-to-`ESCALATE` is portable and enough.

## Consequences

- ✅ Day 3 runs end to end on a real model; every later day (budgets,
  LLM spans, evals) builds on `Usage` from the interface, not on SDK types.
- ✅ Switching provider or model is configuration plus, at most, one adapter.
- ❌ Alert payloads go to OpenAI (T10). Acceptable only because this build
  uses synthetic data; redaction and zero-retention are phase two.
- ❌ Model behavior changes with the provider's model versions. `LLM_MODEL`
  should name a dated snapshot once evals (Day 25) give a baseline to
  protect.
