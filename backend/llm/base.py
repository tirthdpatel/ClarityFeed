"""Provider-agnostic LLM interface.

Everything downstream of this module is vendor-neutral. A provider is
responsible for its own rate limiting, retries and fallback; callers only
see ``complete()`` returning text or ``None``.

``None`` means "the LLM did not produce usable output". It is never an
exception, because per ARCHITECTURE_V2 §A3 the LLM sits *after* the publish
barrier: enrichment failing must degrade the product, not take it down.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass
class LLMResponse:
    """Result of a completion, including usage for quota accounting."""

    text: Optional[str]
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider: str = ""
    error: Optional[str] = field(default=None)

    @property
    def ok(self) -> bool:
        return self.text is not None and self.text.strip() != ""


@runtime_checkable
class LLMProvider(Protocol):
    """The interface every provider implements."""

    name: str

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.2,
        expect_json: bool = False,
    ) -> Optional[str]:
        """Return completion text, or ``None`` if the call failed."""
        ...

    async def complete_detailed(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.2,
        expect_json: bool = False,
    ) -> LLMResponse:
        """Return the full response including token usage."""
        ...


_JSON_INSTRUCTION = (
    " Respond ONLY with a valid JSON object. Do not include any text outside the JSON."
)


def apply_json_instruction(system_prompt: str) -> str:
    """Append the JSON-only instruction to a system prompt."""
    return system_prompt + _JSON_INSTRUCTION


def strip_json_fences(text: str) -> str:
    """Remove markdown code fences that models wrap JSON in.

    Shared by every provider — they all do this, and they all do it wrong
    in slightly different ways if each one implements it separately.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    if stripped.endswith("```"):
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()
