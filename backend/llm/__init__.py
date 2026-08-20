"""LLM provider abstraction.

Why this package exists
-----------------------
Groq has deprecated models out from under this project twice:
``mixtral-8x7b-32768`` in March 2025 and ``llama-3.1-8b-instant`` on
16 August 2026. Both were named directly in ``config/settings.py`` and both
now return 404 rather than a deprecation warning.

Callers (summarizer, categorizer) should depend on the :class:`LLMProvider`
protocol, never on a vendor SDK. Swapping or adding a provider is then a
config change plus one new module, and the next deprecation is a one-line fix.

Usage::

    from backend.llm import get_provider

    provider = get_provider()
    text = await provider.complete(system_prompt, user_prompt, expect_json=True)
"""

from backend.llm.base import LLMProvider, LLMResponse
from backend.llm.factory import get_provider, reset_provider_cache

__all__ = ["LLMProvider", "LLMResponse", "get_provider", "reset_provider_cache"]
