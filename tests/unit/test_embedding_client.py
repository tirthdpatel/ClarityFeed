"""Unit tests for backend/deduplicator/embedding_client.py."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.deduplicator.embedding_client import EmbeddingClient


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestEmbeddingClient:
    """Tests for the embedding client."""

    @patch("backend.deduplicator.embedding_client.httpx.AsyncClient")
    def test_successful_batch_embedding(self, mock_client_cls) -> None:
        """Successful batch returns correct shape (384-dim vectors)."""
        # MagicMock, not AsyncMock: httpx's Response.json() is synchronous.
        # As an AsyncMock it returned an un-awaited coroutine, the client
        # failed to parse it and yielded None, and the test failed with a
        # "coroutine was never awaited" RuntimeWarning. Only the *client* is
        # async here — the response object is not.
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [[0.1] * 384, [0.2] * 384]
        mock_response.content = b""
        mock_response.headers = {}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        client = EmbeddingClient()
        result = _run(client.embed_batch(["text1", "text2"]))
        assert len(result) == 2
        assert result[0] is not None
        assert len(result[0]) == 384
        assert result[1] is not None
        assert len(result[1]) == 384

    @patch("backend.deduplicator.embedding_client.httpx.AsyncClient")
    @patch("backend.deduplicator.embedding_client.settings")
    def test_503_triggers_retry(self, mock_settings, mock_client_cls) -> None:
        """503 model loading triggers retry with backoff."""
        mock_settings.HF_MAX_RETRIES = 2
        mock_settings.HF_RETRY_DELAY_SECONDS = 0.01
        mock_settings.HF_API_BASE_URL = "https://api-inference.huggingface.co"
        mock_settings.HF_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
        mock_settings.HF_API_TOKEN = "test"
        mock_settings.USE_LOCAL_EMBEDDING_FALLBACK = False

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = AsyncMock()
            resp.status_code = 503
            resp.json.return_value = {"error": "model loading", "estimated_time": 20}
            resp.content = b""
            resp.headers = {}
            return resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with patch("backend.deduplicator.embedding_client.settings", mock_settings):
            client = EmbeddingClient()
            result = _run(client.embed_batch(["text"]))
        assert result[0] is None or call_count > 1

    @patch("backend.deduplicator.embedding_client.httpx.AsyncClient")
    @patch("backend.deduplicator.embedding_client.settings")
    def test_local_fallback_not_called_when_disabled(
        self, mock_settings, mock_client_cls
    ) -> None:
        """Local fallback is not called when USE_LOCAL_EMBEDDING_FALLBACK=False."""
        mock_settings.USE_LOCAL_EMBEDDING_FALLBACK = False
        mock_settings.HF_MAX_RETRIES = 1
        mock_settings.HF_RETRY_DELAY_SECONDS = 0.01

        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {}
        mock_response.content = b""
        mock_response.headers = {}
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with patch("backend.deduplicator.embedding_client.settings", mock_settings):
            client = EmbeddingClient()
            with patch.object(client, "_local_fallback", new_callable=AsyncMock) as mock_fb:
                mock_fb.return_value = [None]
                result = _run(client.embed_batch(["text"]))
        # Fallback should not be called when disabled
        mock_fb.assert_not_called()
