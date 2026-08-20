"""Deduplication engine module."""

from backend.deduplicator.deduplicator import Deduplicator, DeduplicationResult
from backend.deduplicator.embedding_client import EmbeddingClient

__all__ = ["Deduplicator", "EmbeddingClient", "DeduplicationResult"]
