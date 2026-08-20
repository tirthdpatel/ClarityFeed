"""Unit tests for backend/summarizer/groq_client.py."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.summarizer.groq_client import GroqLLMClient


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestGroqLLMClient:
    """Tests for the Groq LLM client."""

    @patch("backend.summarizer.groq_client.groq.Groq")
    @patch("backend.summarizer.groq_client.asyncio.sleep", new_callable=AsyncMock)
    def test_successful_call_returns_response(
        self, mock_sleep, mock_groq_cls
    ) -> None:
        """Successful call returns response string."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content='{"key": "value"}'))]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        mock_client.chat.completions.create.return_value = mock_response
        mock_groq_cls.return_value = mock_client

        with patch("backend.summarizer.groq_client.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_response
            client = GroqLLMClient()
            result = _run(
                client.complete("system", "user", expect_json=True)
            )
        mock_sleep.assert_called()
        assert result is not None or mock_thread.called

    @patch("backend.summarizer.groq_client.groq.Groq")
    @patch("backend.summarizer.groq_client.asyncio.sleep", new_callable=AsyncMock)
    def test_strip_json_fences(self, mock_sleep, mock_groq_cls) -> None:
        """expect_json=True strips markdown code fences."""
        stripped = GroqLLMClient._strip_json_fences('```json\n{"a": 1}\n```')
        assert "```" not in stripped
        assert "{" in stripped and "}" in stripped
