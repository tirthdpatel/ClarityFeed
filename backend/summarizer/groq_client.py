"""Groq LLM client — DEPRECATED compatibility shim.

.. deprecated:: Phase 0
    Use :func:`backend.llm.get_provider` instead. This module remains so that
    existing call sites and tests keep working; new code must depend on the
    vendor-neutral :class:`~backend.llm.base.LLMProvider` protocol, not on a
    Groq-specific class.

    ``GroqLLMClient`` satisfies ``LLMProvider`` structurally, so it can be
    passed anywhere a provider is expected during the transition.

The reason for the move is in ARCHITECTURE_V3.md §A1: Groq has deprecated
models out from under this project twice, and both models previously named in
``config/settings.py`` now return 404.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import groq
from groq import APIStatusError, RateLimitError

from backend.llm.base import (
    LLMResponse,
    apply_json_instruction,
    strip_json_fences,
)
from config.settings import settings

logger = logging.getLogger("news.summarizer.groq_client")


class GroqLLMClient:
    """Async-compatible Groq API client with rate limit and fallback support.

    Retained for backwards compatibility. Prefer
    ``backend.llm.get_provider()``.
    """

    name = "groq"

    def __init__(self) -> None:
        self._client = groq.Groq(api_key=settings.GROQ_API_KEY)
        self._primary_model = settings.GROQ_MODEL_PRIMARY
        self._fallback_model = settings.GROQ_MODEL_FALLBACK

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.2,
        expect_json: bool = False,
    ) -> Optional[str]:
        """Complete a chat completion with rate limit and fallback."""
        result = await self.complete_detailed(
            system_prompt, user_prompt, max_tokens, temperature, expect_json
        )
        return result.text

    async def complete_detailed(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.2,
        expect_json: bool = False,
    ) -> LLMResponse:
        """Complete and return token usage alongside the text."""
        await asyncio.sleep(settings.GROQ_INTER_REQUEST_DELAY_SECONDS)

        if expect_json:
            system_prompt = apply_json_instruction(system_prompt)

        try:
            return await self._invoke(
                self._primary_model, system_prompt, user_prompt,
                max_tokens, temperature, expect_json,
            )
        except (RateLimitError, APIStatusError) as e:
            logger.warning(
                "Primary model %s failed: %s — trying fallback", self._primary_model, e
            )
            await asyncio.sleep(settings.GROQ_INTER_REQUEST_DELAY_SECONDS * 2)
            try:
                return await self._invoke(
                    self._fallback_model, system_prompt, user_prompt,
                    max_tokens, temperature, expect_json,
                )
            except Exception as fallback_err:  # noqa: BLE001 - must never raise
                logger.error(
                    "Fallback model %s also failed: %s", self._fallback_model, fallback_err
                )
                return LLMResponse(
                    text=None, provider=self.name, model=self._fallback_model,
                    error=str(fallback_err),
                )
        except Exception as e:  # noqa: BLE001 - must never raise
            logger.error("Groq API error: %s", e, exc_info=True)
            return LLMResponse(
                text=None, provider=self.name, model=self._primary_model, error=str(e)
            )

    async def _invoke(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        expect_json: bool,
    ) -> LLMResponse:
        result = await asyncio.to_thread(
            self._client.chat.completions.create,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = result.choices[0].message.content if result.choices else None
        if text and expect_json:
            text = strip_json_fences(text)

        usage = getattr(result, "usage", None)
        if usage:
            logger.debug(
                "Groq %s: prompt_tokens=%s completion_tokens=%s",
                model,
                getattr(usage, "prompt_tokens", 0),
                getattr(usage, "completion_tokens", 0),
            )
        return LLMResponse(
            text=text,
            model=model,
            provider=self.name,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )

    @staticmethod
    def _strip_json_fences(text: str) -> str:
        """Remove markdown code fences around JSON.

        Delegates to :func:`backend.llm.base.strip_json_fences`.
        """
        return strip_json_fences(text)
