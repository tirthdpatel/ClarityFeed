"""Provider selection.

``get_provider()`` returns the provider named by ``settings.LLM_PROVIDER``.
The instance is cached because constructing a vendor SDK client per call is
wasteful; call :func:`reset_provider_cache` in tests.
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.llm.base import LLMProvider
from config.settings import settings

logger = logging.getLogger("news.llm.factory")

_cached_provider: Optional[LLMProvider] = None


def get_provider(name: Optional[str] = None) -> LLMProvider:
    """Return the configured LLM provider.

    Args:
        name: Override ``settings.LLM_PROVIDER``. Mainly for tests.

    Raises:
        ValueError: if the provider name is not recognised. This is a
            configuration error and should fail loudly at startup rather
            than silently at 3am during an ingestion run.
    """
    global _cached_provider

    provider_name = (name or settings.LLM_PROVIDER).strip().lower()

    if _cached_provider is not None and getattr(_cached_provider, "name", None) == provider_name:
        return _cached_provider

    if provider_name == "groq":
        from backend.llm.groq_provider import GroqProvider

        _cached_provider = GroqProvider()
    elif provider_name == "gemini":
        # Phase 5. Deliberately not stubbed with a fake implementation —
        # a provider that silently returns None is worse than one that
        # refuses to start.
        raise ValueError(
            "The Gemini provider is not implemented yet (planned for Phase 5). "
            "Set LLM_PROVIDER=groq."
        )
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER {provider_name!r}. Supported: 'groq' (and 'gemini' in Phase 5)."
        )

    logger.info("LLM provider initialised: %s", provider_name)
    return _cached_provider


def reset_provider_cache() -> None:
    """Clear the cached provider. For tests and config reloads."""
    global _cached_provider
    _cached_provider = None
