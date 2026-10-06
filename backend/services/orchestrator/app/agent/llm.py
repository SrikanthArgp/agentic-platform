"""The agent's chat model (ADR-0022).

The agent talks to a LangChain chat model, so the provider is a
constructor swap here, never a change to the loop. Provider SDKs read
their own API key (`OPENAI_API_KEY`); it never passes through platform
code.

`LLMError` is the one error callers see for any model-call failure
(network, auth, rate limit, bad response): `ModelErrors` middleware
turns whatever the provider raised into it.
"""

import logging

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

MODEL_TIMEOUT_S = 60.0
MODEL_RETRIES = 2


class LLMError(Exception):
    """The provider call failed (network, auth, rate limit, bad response)."""


def build_chat_model(provider: str, model: str) -> BaseChatModel:
    if provider == "openai":
        # Imported here so tests never need the provider configured.
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, timeout=MODEL_TIMEOUT_S, max_retries=MODEL_RETRIES)
    raise ValueError(f"Unsupported LLM_PROVIDER '{provider}'.")


class ModelErrors(AgentMiddleware):
    """Any exception from the model call -> `LLMError`."""

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        try:
            return await handler(request)
        except LLMError:
            raise
        except Exception as e:
            logger.warning("LLM call failed: %s: %s", type(e).__name__, e)
            raise LLMError(f"{type(e).__name__}: {e}") from e
