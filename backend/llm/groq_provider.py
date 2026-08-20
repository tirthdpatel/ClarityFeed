"""Groq implementation of :class:`~backend.llm.base.LLMProvider`.

Free tier as of 2026-08-19: 30 RPM / 1,000 RPD / 8K TPM / 200K TPD.
Requests-per-day is the binding constraint. See ARCHITECTURE_V3.md §A1.
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

logger = logging.getLogger("news.llm.groq")


class GroqProvider:
    """Groq API provider with rate limiting and primary/fallback models."""

    name = "groq"

    def __init__(
        self,
        api_key: Optional[str] = None,
        primary_model: Optional[str] = None,
        fallback_model: Optional[str] = None,
    ) -> None:
        self._client = groq.Groq(api_key=api_key or settings.GROQ_API_KEY)
        self._primary_model = primary_model or settings.GROQ_MODEL_PRIMARY
        self._fallback_model = fallback_model or settings.GROQ_MODEL_FALLBACK

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.2,
        expect_json: bool = False,
    ) -> Optional[str]:
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
        await asyncio.sleep(settings.GROQ_INTER_REQUEST_DELAY_SECONDS)

        if expect_json:
            system_prompt = apply_json_instruction(system_prompt)

        try:
            return await self._call(
                self._primary_model, system_prompt, user_prompt,
                max_tokens, temperature, expect_json,
            )
        except (RateLimitError, APIStatusError) as e:
            logger.warning(
                "Groq primary model %s failed: %s — trying fallback %s",
                self._primary_model, e, self._fallback_model,
            )
            await asyncio.sleep(settings.GROQ_INTER_REQUEST_DELAY_SECONDS * 2)
            try:
                return await self._call(
                    self._fallback_model, system_prompt, user_prompt,
                    max_tokens, temperature, expect_json,
                )
            except Exception as fallback_err:  # noqa: BLE001 - must never raise
                logger.error(
                    "Groq fallback model %s also failed: %s",
                    self._fallback_model, fallback_err,
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

    async def _call(
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
        return LLMResponse(
            text=text,
            model=model,
            provider=self.name,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
