"""
SQLAlchemy ORM models for ClarityFeed — Neon PostgreSQL compatible.

NEON-SPECIFIC ENGINE CONFIGURATION NOTES:
    1. Neon suspends after 5 minutes of inactivity. ``pool_pre_ping=True``
       issues a ``SELECT 1`` before every connection use to detect and
       recover dead connections after Neon auto-resume.
    2. ``pool_recycle=300`` discards connections older than 5 minutes to
       force reconnection after Neon idle suspension.
    3. ``connect_args={"sslmode": "require", "connect_timeout": 10}`` — SSL
       is mandatory for Neon; connect_timeout handles resume latency.

EMBEDDING STORAGE:
    Embeddings are stored as JSON-serialized Text, *not* pgvector.
    pgvector requires the Neon Pro plan. Using JSON text avoids this
    cost while still enabling cosine similarity via scikit-learn after
    deserialisation with ``json.loads()``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship

from backend.database.models import PipelineStatus

# ---------------------------------------------------------------------------
# Publish-barrier states (migration 004).
#
# These were declared here as String(12) with nullable=True, which did not
# match the database: migration 004 creates two PostgreSQL ENUM types and
# makes both columns NOT NULL with no server default. The mismatch is not
# cosmetic — an INSERT that omitted them raised NotNullViolation, so every
# insert into a migrated database failed. The ORM now carries the real type
# and a Python-side default, which fixes every insert path at once rather
# than requiring each call site to remember.
# ---------------------------------------------------------------------------

INGEST_STATES = ("PENDING", "FETCHED", "CLEANED", "PUBLISHED", "FAILED")
ENRICH_STATES = ("PENDING", "DONE", "FAILED", "SKIPPED")


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    pass


class Source(Base):
    """An RSS feed source.

    ``seed_default_sources()`` inserts the initial list on first deployment
    to Render via the FastAPI startup event.
    """

    __tablename__ = "sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    url = Column(String(2048), nullable=False, comment="Publisher website URL")
    feed_url = Column(String(2048), nullable=False, unique=True, comment="RSS/Atom feed URL")
    language = Column(String(10), nullable=False, default="en")
    country = Column(String(100), nullable=False, default="")
    category = Column(String(100), nullable=False, default="general")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Phase 2 (migration 004): denormalised health counters so the admin
    # dashboard does not aggregate source_health on every page load.
    last_success_at = Column(DateTime, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    # Drives canonical-article selection in clustering: one wire-service story
    # is not equivalent to one blog post.
    editorial_weight = Column(Float, nullable=False, default=1.0)

    raw_articles = relationship("RawArticle", back_populates="source", lazy="dynamic")

    def __repr__(self) -> str:
        return f"<Source(id={self.id}, name={self.name!r})>"


class RawArticle(Base):
    """A raw article as ingested from an RSS feed.

    ``raw_html`` is nullable and should be set to ``NULL`` after content
    cleaning to conserve Neon's 0.5 GB storage limit.
    """

    __tablename__ = "raw_articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False, index=True)
    # A4 FIX: the unique constraint moved from `url` to `url_hash`.
    #
    # `String(2048) unique=True` produced a btree index over a value that can
    # exceed PostgreSQL's ~2,704-byte index-entry limit once multi-byte UTF-8
    # is involved — i.e. it worked in SQLite tests and threw in production on
    # the first long Japanese or Arabic slug. `url` is now plain Text with no
    # index; dedup happens on the fixed-width hash below.
    url = Column(Text, nullable=False, comment="Original article URL as published")
    url_hash = Column(
        String(64),
        nullable=True,  # nullable during migration backfill; NOT NULL in rev 003
        unique=True,
        index=True,
        comment="SHA-256 of the canonical URL — see backend/urls.py",
    )
    title = Column(String(1024), nullable=False)
    published_at = Column(DateTime, nullable=True)
    # STORAGE CONSERVATION: cleared to NULL after cleaning to respect Neon free tier storage limit
    raw_html = Column(Text, nullable=True)
    summary_from_feed = Column(Text, nullable=True)
    status = Column(
        Enum(PipelineStatus, name="pipeline_status", create_constraint=True),
        nullable=False,
        default=PipelineStatus.PENDING,
        index=True,
    )
    # Phase 2 (migration 004) — the publish barrier, V2 §A3.
    # `status` above is a shadow column kept for one release. Read these.
    ingest_status = Column(
        Enum(*INGEST_STATES, name="ingest_status", create_constraint=False),
        nullable=False,
        default=INGEST_STATES[0],
        index=True,
    )
    enrichment_status = Column(
        Enum(*ENRICH_STATES, name="enrichment_status", create_constraint=False),
        nullable=False,
        default=ENRICH_STATES[0],
        index=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    source = relationship("Source", back_populates="raw_articles")
    cleaned_article = relationship("CleanedArticle", back_populates="raw_article", uselist=False)
    embedding = relationship("Embedding", back_populates="raw_article", uselist=False)
    summary = relationship("Summary", back_populates="raw_article", uselist=False)
    category = relationship("Category", back_populates="raw_article", uselist=False)

    def __repr__(self) -> str:
        return f"<RawArticle(id={self.id}, url={self.url!r})>"


class CleanedArticle(Base):
    """Cleaned and extracted article body."""

    __tablename__ = "cleaned_articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_article_id = Column(Integer, ForeignKey("raw_articles.id"), nullable=False, unique=True)
    clean_text = Column(Text, nullable=False)
    word_count = Column(Integer, nullable=False, default=0)
    language = Column(String(10), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    raw_article = relationship("RawArticle", back_populates="cleaned_article")

    def __repr__(self) -> str:
        return f"<CleanedArticle(id={self.id}, raw_article_id={self.raw_article_id})>"


class Embedding(Base):
    """Semantic embedding vector for an article.

    The vector is stored as JSON-serialised Text (not pgvector, which
    requires Neon Pro). Use ``json.loads(vector_json)`` and scikit-learn's
    ``cosine_similarity`` for deduplication in Phase 2.
    """

    __tablename__ = "embeddings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_article_id = Column(Integer, ForeignKey("raw_articles.id"), nullable=False, unique=True)
    # JSON-serialised List[float] — 384 dimensions for all-MiniLM-L6-v2
    vector_json = Column(Text, nullable=True)
    model_name = Column(String(255), nullable=False, default="sentence-transformers/all-MiniLM-L6-v2")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    raw_article = relationship("RawArticle", back_populates="embedding")

    def __repr__(self) -> str:
        return f"<Embedding(id={self.id}, raw_article_id={self.raw_article_id})>"


class Summary(Base):
    """AI-generated summary of an article.

    Phase 2 adds tldr and bullet_points for structured summarization.
    summary_text remains for backward compatibility (stores full JSON when using Phase 2).
    """

    __tablename__ = "summaries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_article_id = Column(Integer, ForeignKey("raw_articles.id"), nullable=False, unique=True)
    summary_text = Column(Text, nullable=False)  # TLDR when tldr is empty; full JSON in Phase 2
    tldr = Column(Text, nullable=True)  # Phase 2: one-sentence summary
    bullet_points = Column(Text, nullable=True)  # Phase 2: JSON-serialized List[str]
    model_name = Column(String(255), nullable=False, default="")
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    raw_article = relationship("RawArticle", back_populates="summary")

    def __repr__(self) -> str:
        return f"<Summary(id={self.id}, raw_article_id={self.raw_article_id})>"


class Category(Base):
    """AI-assigned category for an article."""

    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_article_id = Column(Integer, ForeignKey("raw_articles.id"), nullable=False, unique=True)
    primary_category = Column(String(255), nullable=False, default="")
    secondary_category = Column(String(255), nullable=True)
    confidence = Column(Float, nullable=False, default=0.0)
    model_name = Column(String(255), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    raw_article = relationship("RawArticle", back_populates="category")

    def __repr__(self) -> str:
        return f"<Category(id={self.id}, raw_article_id={self.raw_article_id})>"


# ===========================================================================
# PHASE 1 — geography, taxonomy, permissions, languages
#
# Everything below is added by migrations 003–007. See ARCHITECTURE_V2 §3 and
# ARCHITECTURE_V3 Parts B and C.
# ===========================================================================


class Region(Base):
    """A world region — the top level of the geography hierarchy."""

    __tablename__ = "regions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(50), nullable=False, unique=True)
    name = Column(String(100), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)

    countries = relationship("Country", back_populates="region")

    def __repr__(self) -> str:
        return f"<Region(code={self.code!r})>"


class Country(Base):
    """A country. Adding one is rows in this table plus `country_aliases` —
    no code change and no redeploy, which is what the brief required."""

    __tablename__ = "countries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    iso2 = Column(String(2), nullable=False, unique=True, index=True)
    iso3 = Column(String(3), nullable=False)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    region_id = Column(Integer, ForeignKey("regions.id"), nullable=True, index=True)
    default_language = Column(String(10), nullable=False, default="en")
    flag_emoji = Column(String(16), nullable=True)
    is_enabled = Column(Boolean, nullable=False, default=False, index=True)
    sort_order = Column(Integer, nullable=False, default=0)
    # Denormalised counters so the country selector does not COUNT(*) per country.
    cached_article_count = Column(Integer, nullable=False, default=0)
    cached_at = Column(DateTime, nullable=True)

    region = relationship("Region", back_populates="countries")
    aliases = relationship("CountryAlias", back_populates="country", lazy="dynamic")

    def __repr__(self) -> str:
        return f"<Country(iso2={self.iso2!r}, name={self.name!r})>"


class CountryAlias(Base):
    """A surface string that indicates a country.

    This table is the engine of §8's classifier. `is_ambiguous` + `requires_context`
    are what stop "Georgia" the US state from being filed under Georgia the country:
    an ambiguous alias scores nothing unless one of its context terms is present.
    """

    __tablename__ = "country_aliases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    country_id = Column(Integer, ForeignKey("countries.id"), nullable=False, index=True)
    alias = Column(String(200), nullable=False, index=True)
    alias_type = Column(String(20), nullable=False, default="name")
    weight = Column(Float, nullable=False, default=1.0)
    is_ambiguous = Column(Boolean, nullable=False, default=False)
    # JSON-encoded list of disambiguating terms. Stored as Text rather than
    # ARRAY so the schema stays portable to SQLite for the test suite.
    requires_context = Column(Text, nullable=True)

    country = relationship("Country", back_populates="aliases")

    def __repr__(self) -> str:
        return f"<CountryAlias(alias={self.alias!r})>"


class CategoryDef(Base):
    """The category taxonomy. Self-referencing parent gives Technology -> AI
    without a second table (ARCHITECTURE_V3 Part C)."""

    __tablename__ = "category_defs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    parent_id = Column(Integer, ForeignKey("category_defs.id"), nullable=True, index=True)
    description = Column(Text, nullable=True)
    keywords = Column(Text, nullable=True)  # JSON-encoded list, drives the rule classifier
    color_token = Column(String(50), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    is_enabled = Column(Boolean, nullable=False, default=True)

    children = relationship("CategoryDef", backref="parent", remote_side=[id])

    def __repr__(self) -> str:
        return f"<CategoryDef(slug={self.slug!r})>"


class Language(Base):
    """A language a reader can choose (ARCHITECTURE_V3 §B4)."""

    __tablename__ = "languages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    # A switcher that shows "Hindi" to someone who only reads Hindi is broken.
    native_name = Column(String(100), nullable=False)
    direction = Column(String(3), nullable=False, default="ltr")
    translation_tier = Column(String(10), nullable=False, default="argos")
    is_enabled = Column(Boolean, nullable=False, default=False, index=True)
    sort_order = Column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<Language(code={self.code!r}, dir={self.direction})>"


class SourcePermission(Base):
    """What a publisher permits. The legal core — every default is restrictive
    (ARCHITECTURE_V2 §3.6). A source added without review contributes a
    headline, a short excerpt and a link, and nothing more."""

    __tablename__ = "source_permissions"

    source_id = Column(Integer, ForeignKey("sources.id"), primary_key=True)
    can_store_title = Column(Boolean, nullable=False, default=True)
    can_store_description = Column(Boolean, nullable=False, default=True)
    can_store_full_text = Column(Boolean, nullable=False, default=False)
    can_store_image = Column(Boolean, nullable=False, default=False)
    can_generate_summary = Column(Boolean, nullable=False, default=True)
    # Translation is a derivative work; defaults true because it sits closer to
    # summarisation (allowed) than reproduction (denied). See V3 §B8.
    can_translate = Column(Boolean, nullable=False, default=True)
    requires_attribution = Column(Boolean, nullable=False, default=True)
    original_url_required = Column(Boolean, nullable=False, default=True)
    max_description_chars = Column(Integer, nullable=False, default=300)
    robots_override = Column(Boolean, nullable=False, default=False)
    terms_url = Column(Text, nullable=True)
    robots_url = Column(Text, nullable=True)
    licence_note = Column(Text, nullable=True)
    # NULL reviewed_at is itself a state the admin panel surfaces:
    # "14 sources have never been permission-reviewed."
    reviewed_by = Column(String(255), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_notes = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<SourcePermission(source_id={self.source_id})>"


class ArticleCountry(Base):
    """Article <-> country, many-to-many.

    `published_at` is DENORMALISED here on purpose. "Latest articles for
    country X" is the hottest query in the product; with a composite index on
    (country_id, relevance, published_at DESC) it becomes an index-only range
    scan that stops after 30 rows instead of a join-then-sort over the whole
    table. See ARCHITECTURE_V2 §3.4 — the most important index decision here.
    """

    __tablename__ = "article_countries"

    article_id = Column(Integer, ForeignKey("raw_articles.id"), primary_key=True)
    country_id = Column(Integer, ForeignKey("countries.id"), primary_key=True)
    relevance = Column(String(12), nullable=False, default="primary")
    confidence = Column(Float, nullable=False, default=0.0)
    method = Column(String(12), nullable=False, default="gazetteer")
    published_at = Column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<ArticleCountry(a={self.article_id}, c={self.country_id})>"


class ArticleCategory(Base):
    """Article <-> category, many-to-many. An AI company's IPO is legitimately
    both `ai` and `markets`; `is_primary` decides which one leads."""

    __tablename__ = "article_categories"

    article_id = Column(Integer, ForeignKey("raw_articles.id"), primary_key=True)
    category_id = Column(Integer, ForeignKey("category_defs.id"), primary_key=True)
    confidence = Column(Float, nullable=False, default=0.0)
    is_primary = Column(Boolean, nullable=False, default=False)
    method = Column(String(12), nullable=False, default="rule")

    def __repr__(self) -> str:
        return f"<ArticleCategory(a={self.article_id}, c={self.category_id})>"


class ArticleTranslation(Base):
    """Translated title/description per language (ARCHITECTURE_V3 §B4).

    Populated after the publish barrier, so a translation failure degrades the
    reader's experience rather than taking the site down.
    """

    __tablename__ = "article_translations"

    article_id = Column(Integer, ForeignKey("raw_articles.id"), primary_key=True)
    language_id = Column(Integer, ForeignKey("languages.id"), primary_key=True)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    method = Column(String(12), nullable=False, default="argos")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<ArticleTranslation(a={self.article_id}, l={self.language_id})>"


# ===========================================================================
# PHASE 2 — publish barrier and operational tables (migration 004)
# ===========================================================================


class IngestionRun(Base):
    """One execution of the ingestion pipeline.

    Exists so "did the site update?" has an answer that is not "look at the
    articles and guess". The admin dashboard in §14 reads from here.
    """

    __tablename__ = "ingestion_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    trigger = Column(String(12), nullable=False, default="cron")
    status = Column(String(12), nullable=False, default="running")
    sources_attempted = Column(Integer, nullable=False, default=0)
    sources_ok = Column(Integer, nullable=False, default=0)
    sources_failed = Column(Integer, nullable=False, default=0)
    articles_new = Column(Integer, nullable=False, default=0)
    articles_duplicate = Column(Integer, nullable=False, default=0)
    articles_published = Column(Integer, nullable=False, default=0)
    clusters_created = Column(Integer, nullable=False, default=0)
    llm_calls_used = Column(Integer, nullable=False, default=0)
    error_summary = Column(Text, nullable=True)
    git_sha = Column(String(40), nullable=True)

    def __repr__(self) -> str:
        return f"<IngestionRun(id={self.id}, status={self.status!r})>"


class SourceHealth(Base):
    """One health observation for one source.

    ``articles_returned`` is the column that catches A8. A feed answering 200
    with zero entries for six consecutive runs is dead, and auto-disable keys
    off this rather than off HTTP status — the failure mode looks healthy at
    the transport layer.
    """

    __tablename__ = "source_health"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False, index=True)
    checked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    ok = Column(Boolean, nullable=False, default=True)
    http_status = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    articles_returned = Column(Integer, nullable=False, default=0)
    error_type = Column(String(50), nullable=True)
    error_detail = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<SourceHealth(source_id={self.source_id}, ok={self.ok})>"
