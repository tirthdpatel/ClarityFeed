"""
ClarityFeed interface contracts — dataclass definitions and pipeline status enum.

These dataclasses define the input/output contracts between pipeline stages.
All datetime fields default to ``datetime.utcnow``. All Optional fields
default to ``None``.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class PipelineStatus(str, enum.Enum):
    """Status of an article as it moves through the processing pipeline."""

    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"


@dataclass
class SourceRecord:
    """Represents an RSS feed source."""

    id: Optional[int] = None
    name: str = ""
    url: str = ""
    feed_url: str = ""
    language: str = "en"
    country: str = ""
    category: str = "general"
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class RawArticleRecord:
    """Represents a raw article as ingested from an RSS feed."""

    id: Optional[int] = None
    source_id: int = 0
    url: str = ""
    title: str = ""
    published_at: Optional[datetime] = None
    raw_html: Optional[str] = None
    summary_from_feed: Optional[str] = None
    status: PipelineStatus = PipelineStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CleanedArticleRecord:
    """Represents a cleaned and extracted article body."""

    id: Optional[int] = None
    raw_article_id: int = 0
    clean_text: str = ""
    word_count: int = 0
    language: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class EmbeddingRecord:
    """Represents a semantic embedding vector for an article.

    The vector is stored as JSON-serialized List[float] (384 dimensions
    for all-MiniLM-L6-v2). Not using pgvector since it requires Neon Pro.
    """

    id: Optional[int] = None
    raw_article_id: int = 0
    vector_json: Optional[str] = None
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class SummaryRecord:
    """Represents an AI-generated summary of an article."""

    id: Optional[int] = None
    raw_article_id: int = 0
    summary_text: str = ""
    model_name: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CategoryRecord:
    """Represents an AI-assigned category for an article."""

    id: Optional[int] = None
    raw_article_id: int = 0
    primary_category: str = ""
    secondary_category: Optional[str] = None
    confidence: float = 0.0
    model_name: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
