"""
LLM facade for the chat pipeline.

Provider-agnostic interface that currently supports Anthropic (default) and
OpenAI. The Anthropic path takes advantage of:

  * **Prompt caching** — the system prompt is wrapped in a content block
    with `cache_control={"type": "ephemeral"}`. Cached reads cost ~10% of
    fresh input, which dominates per-token cost for chatty avatars that
    share a system prompt across many turns. Workspace-isolated as of
    Anthropic's Feb 2026 change.
  * **Thinking (opt-in)** — when callers pass `thinking=True` the model
    is asked to reason deliberately before answering: adaptive thinking at
    high effort on current models, a token budget on legacy ones. Reserved
    for hard turns; the live conversation path runs at low effort instead,
    because time-to-first-word is what a listener notices.

Exceptions are re-raised as `LLMError` subclasses so the WebSocket pipeline
can distinguish rate-limit / auth / network failures and surface
appropriate user-facing messages.
"""

from __future__ import annotations

import logging
import re
from typing import AsyncGenerator, Dict, List, Optional

import anthropic
import openai

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant in a real-time avatar conversation system. "
    "Keep replies concise and conversational so they can be spoken aloud."
)

# Thinking budget for the models that still take one (see _is_legacy_claude).
# Current models size their own thinking and are steered with `effort`.
_DEFAULT_THINKING_BUDGET = 4096

# Claude 3.x, and 4.0 through 4.6 — optionally with a date suffix.
_LEGACY_CLAUDE_RE = re.compile(r"^claude-(?:3\b|(?:opus|sonnet|haiku)-4(?:-([0-6]))?(?:-\d{8})?$)")


def _is_legacy_claude(model: str) -> bool:
    """
    True for Claude models from before the 4.7 request surface.

    The two generations are steered differently, and each REJECTS the
    other's controls with a 400 — which here means every turn of every
    conversation failing with "the AI provider returned an error":

      legacy (<= 4.6)   thinking takes `budget_tokens`; `effort` is an error
                        on several of them
      current (>= 4.7)  thinking is adaptive and its depth is set with
                        `output_config.effort`; a token budget is rejected

    `temperature` is sent to NEITHER. The current API version rejects
    sampling parameters on every model and the 1.x+ SDK no longer has the
    argument at all — this module used to pass it unconditionally, so on a
    fresh install every Claude request raised a TypeError before it was sent.
    """
    return bool(_LEGACY_CLAUDE_RE.match(model or ""))


class LLMError(Exception):
    """Base class — chat pipeline catches this and emits a typed WS error."""


class LLMRateLimited(LLMError):
    """Provider returned 429."""


class LLMAuthError(LLMError):
    """Provider returned 401/403 — usually a misconfigured API key."""


class LLMUnavailable(LLMError):
    """Network failure, timeout, or 5xx from the provider."""


class LLMRefused(LLMError):
    """The model declined the request (stop_reason == "refusal")."""


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
    if isinstance(
        exc,
        (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.InternalServerError),
    ):
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
    if isinstance(
        exc, (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError)
    ):
        return LLMUnavailable(str(exc))
    return LLMError(str(exc))


class LLMService:
    """LLM Service for AI responses."""

    def __init__(self):
        self.provider = settings.LLM_PROVIDER
        self.model = settings.LLM_MODEL
        self.temperature = settings.LLM_TEMPERATURE
        self.max_tokens = settings.LLM_MAX_TOKENS
        self.effort = settings.LLM_EFFORT or None
        # Opt-in reasoning cap for OpenAI-compatible "thinking" models (e.g.
        # Ollama cloud glm-*:cloud / kimi-*:cloud). "none" disables reasoning
        # so they return normal `content`. Empty → not sent (default behavior).
        self.reasoning_effort = settings.LLM_REASONING_EFFORT or None

        if self.provider == "anthropic":
            self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        elif self.provider == "ollama":
            # Ollama (and vLLM / LM Studio / OpenRouter) speak the OpenAI
            # wire protocol — reuse the OpenAI client against their base URL.
            # Fully local and free; no API key required (the client insists
            # on a non-empty string, so we pass a placeholder).
            base_url = settings.OPENAI_BASE_URL or "http://localhost:11434/v1"
            self.client = openai.AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY or "ollama",
                base_url=base_url,
            )
            self.provider = "openai"  # downstream code paths are identical
            logger.info(f"LLM provider 'ollama' → OpenAI-compatible client at {base_url}")
        elif self.provider == "openai":
            self.client = openai.AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url=settings.OPENAI_BASE_URL,  # None → api.openai.com
            )
        else:
            # Fail here rather than leaving `self.client` unset. Without this a
            # typo in LLM_PROVIDER surfaced much later as an opaque
            # AttributeError from inside the turn pipeline, pointing at the
            # wrong thing entirely.
            raise LLMError(
                f"Unsupported LLM_PROVIDER {self.provider!r}. "
                f"Expected one of: anthropic, openai, ollama."
            )

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
        kwargs = self._anthropic_kwargs(messages, system_prompt, thinking)

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

        if getattr(response, "stop_reason", None) == "refusal":
            raise LLMRefused("The model declined this request")
        raise LLMError("Anthropic response contained no text block")

    def _anthropic_kwargs(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str],
        thinking: bool = False,
    ) -> dict:
        """
        Request parameters for the configured Claude model.

        The one place that knows which generation takes what — see
        _is_legacy_claude. `thinking=True` asks for deliberate reasoning
        (reserved for hard turns; nothing on the live path uses it).
        """
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": _cacheable_system(system_prompt),
            "messages": messages,
        }

        if _is_legacy_claude(self.model):
            if thinking:
                # Thinking tokens count against the output budget, so widen
                # max_tokens to cover both.
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": _DEFAULT_THINKING_BUDGET}
                kwargs["max_tokens"] = self.max_tokens + _DEFAULT_THINKING_BUDGET
            return kwargs

        # Current models think adaptively by default, and how much is governed
        # by effort. Their default effort is tuned for hard problems; for a
        # spoken reply that is seconds of silence before the first word, so
        # the conversational path asks for the configured (low) effort.
        effort = "high" if thinking else self.effort
        if effort:
            kwargs["output_config"] = {"effort": effort}
        if thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        return kwargs

    def _openai_extra(self) -> dict:
        """Extra request params for the OpenAI-compatible path.

        `reasoning_effort` is forwarded via `extra_body` (not a named kwarg) so
        it lands in the raw request body regardless of SDK version and is a
        no-op for servers that ignore it. Needed for Ollama cloud thinking
        models — see LLM_REASONING_EFFORT.
        """
        if self.reasoning_effort:
            return {"extra_body": {"reasoning_effort": self.reasoning_effort}}
        return {}

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
                **self._openai_extra(),
            )
        except Exception as e:
            mapped = _map_openai_exception(e)
            logger.error("openai_call_failed", extra={"error_type": type(e).__name__})
            raise mapped from e

        if not response.choices:
            raise LLMError("OpenAI-compatible response contained no choices")
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
                **self._anthropic_kwargs(messages, system_prompt)
            ) as stream:
                produced = False
                # text_stream yields answer text only — thinking blocks, which
                # current models may emit first, never reach the listener.
                async for text in stream.text_stream:
                    produced = True
                    yield text
                final = await stream.get_final_message()
            self._log_usage(getattr(final, "usage", None), thinking=False)
            # A decline arrives as a normal 200 with stop_reason "refusal",
            # not as an exception. With no text it used to look like a reply
            # that simply said nothing.
            if getattr(final, "stop_reason", None) == "refusal" and not produced:
                raise LLMRefused("The model declined this request")
        except LLMError:
            raise
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
                **self._openai_extra(),
            )
            async for chunk in stream:
                # OpenAI sends a final usage-only chunk with an empty
                # `choices` list when stream_options.include_usage is set, and
                # several OpenAI-compatible servers (Ollama, vLLM, OpenRouter)
                # emit one unconditionally. Indexing [0] blindly raised
                # IndexError on that terminal chunk, failing the turn *after*
                # the user had already seen the whole reply.
                if not chunk.choices:
                    continue
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
