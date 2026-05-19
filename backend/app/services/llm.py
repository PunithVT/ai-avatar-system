"""
LLM facade for the chat pipeline.

Provider-agnostic interface that currently supports Anthropic (default) and
OpenAI. The Anthropic path takes advantage of:

  * **Prompt caching** — the system prompt is wrapped in a content block
    with `cache_control={"type": "ephemeral"}`. Cached reads cost ~10% of
    fresh input, which dominates per-token cost for chatty avatars that
    share a system prompt across many turns. Workspace-isolated as of
    Anthropic's Feb 2026 change.
  * **Extended thinking (opt-in)** — when callers pass `thinking=True`
    we set `thinking={"type": "enabled", "budget_tokens": ...}` so the
    model reasons internally before answering. Reserved for hard turns;
    using it on every turn would multiply token cost.

Exceptions are re-raised as `LLMError` subclasses so the WebSocket pipeline
can distinguish rate-limit / auth / network failures and surface
appropriate user-facing messages.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator, Dict, List, Optional

import anthropic
import openai

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant in a real-time avatar conversation system. "
    "Keep replies concise and conversational so they can be spoken aloud."
)

# Extended-thinking budget. Claude 4.x Opus supports up to 128k thinking
# tokens; for an interactive avatar we want responses fast, so we cap the
# budget low. Increase for research/agentic use cases.
_DEFAULT_THINKING_BUDGET = 4096


class LLMError(Exception):
    """Base class — chat pipeline catches this and emits a typed WS error."""


class LLMRateLimited(LLMError):
    """Provider returned 429."""


class LLMAuthError(LLMError):
    """Provider returned 401/403 — usually a misconfigured API key."""


class LLMUnavailable(LLMError):
    """Network failure, timeout, or 5xx from the provider."""


def _cacheable_system(system_prompt: Optional[str]) -> list[dict]:
    """
    Build a system block list with prompt-cache marking applied to the
    (long-lived) system prompt. The SDK accepts either a plain string OR
    a list of blocks; blocks are needed to attach `cache_control` per-block.
    """
    text = system_prompt or DEFAULT_SYSTEM_PROMPT
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _map_anthropic_exception(exc: Exception) -> LLMError:
    """Translate Anthropic SDK exceptions into the typed LLMError hierarchy."""
    if isinstance(exc, anthropic.RateLimitError):
        return LLMRateLimited(str(exc))
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return LLMAuthError(str(exc))
    if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.InternalServerError)):
        return LLMUnavailable(str(exc))
    if isinstance(exc, anthropic.BadRequestError):
        # 400 from Anthropic is usually our bug, not theirs — surface verbatim.
        return LLMError(f"Invalid request to Anthropic: {exc}")
    return LLMError(str(exc))


def _map_openai_exception(exc: Exception) -> LLMError:
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimited(str(exc))
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return LLMAuthError(str(exc))
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError)):
        return LLMUnavailable(str(exc))
    return LLMError(str(exc))


class LLMService:
    """LLM Service for AI responses."""

    def __init__(self):
        self.provider = settings.LLM_PROVIDER
        self.model = settings.LLM_MODEL
        self.temperature = settings.LLM_TEMPERATURE
        self.max_tokens = settings.LLM_MAX_TOKENS

        if self.provider == "anthropic":
            self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        elif self.provider == "openai":
            self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    # ── non-streaming ────────────────────────────────────────────────────────

    async def generate_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        thinking: bool = False,
    ) -> str:
        if self.provider == "anthropic":
            return await self._generate_anthropic(messages, system_prompt, thinking)
        if self.provider == "openai":
            return await self._generate_openai(messages, system_prompt)
        raise LLMError(f"Unsupported LLM provider: {self.provider}")

    async def _generate_anthropic(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str],
        thinking: bool,
    ) -> str:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": _cacheable_system(system_prompt),
            "messages": messages,
        }
        # Extended thinking — model "thinks" privately before answering. The
        # thinking tokens still count against output budget so we widen
        # max_tokens to cover both. Per Anthropic docs, temperature must be 1
        # when extended thinking is enabled.
        if thinking:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": _DEFAULT_THINKING_BUDGET}
            kwargs["max_tokens"] = max(self.max_tokens + _DEFAULT_THINKING_BUDGET, self.max_tokens)
            kwargs["temperature"] = 1.0

        try:
            response = await self.client.messages.create(**kwargs)
        except Exception as e:
            mapped = _map_anthropic_exception(e)
            logger.error(
                "anthropic_call_failed",
                extra={"error_type": type(e).__name__, "mapped": type(mapped).__name__},
            )
            raise mapped from e

        # Find the first text block (skip thinking blocks if any).
        for block in response.content or []:
            if getattr(block, "type", None) == "text" and hasattr(block, "text"):
                self._log_usage(response.usage, thinking)
                return block.text

        raise LLMError("Anthropic response contained no text block")

    async def _generate_openai(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> str:
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}] + messages

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as e:
            mapped = _map_openai_exception(e)
            logger.error("openai_call_failed", extra={"error_type": type(e).__name__})
            raise mapped from e

        return response.choices[0].message.content or ""

    # ── streaming ────────────────────────────────────────────────────────────

    async def stream_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        if self.provider == "anthropic":
            async for chunk in self._stream_anthropic(messages, system_prompt):
                yield chunk
        elif self.provider == "openai":
            async for chunk in self._stream_openai(messages, system_prompt):
                yield chunk
        else:
            raise LLMError(f"Unsupported LLM provider: {self.provider}")

    async def _stream_anthropic(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        try:
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=_cacheable_system(system_prompt),
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            mapped = _map_anthropic_exception(e)
            logger.error("anthropic_stream_failed", extra={"error_type": type(e).__name__})
            raise mapped from e

    async def _stream_openai(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}] + messages

        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stream=True,
            )
            async for chunk in stream:
                content = chunk.choices[0].delta.content
                if content:
                    yield content
        except Exception as e:
            mapped = _map_openai_exception(e)
            logger.error("openai_stream_failed", extra={"error_type": type(e).__name__})
            raise mapped from e

    # ── helpers ──────────────────────────────────────────────────────────────

    def _log_usage(self, usage, thinking: bool) -> None:
        if usage is None:
            return
        try:
            logger.info(
                "llm_usage",
                extra={
                    "in_tokens": getattr(usage, "input_tokens", 0),
                    "out_tokens": getattr(usage, "output_tokens", 0),
                    "cache_create_tokens": getattr(usage, "cache_creation_input_tokens", 0),
                    "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0),
                    "thinking": thinking,
                },
            )
        except Exception:
            pass


# Global instance
llm_service = LLMService()
