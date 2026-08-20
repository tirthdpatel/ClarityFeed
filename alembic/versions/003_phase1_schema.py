"""Phase 1 — geography, taxonomy, permissions, languages, translations.

Revision ID: 003_phase1_schema
Revises: 002_add_url_hash
Create Date: 2026-08-19

Creates, in dependency order:

    regions              world regions
    countries            -> regions
    country_aliases      -> countries    the classifier's gazetteer
    category_defs        self-referencing taxonomy (Technology -> AI)
    languages            reader-selectable languages
    source_permissions   -> sources      1:1, restrictive defaults
    article_countries    -> raw_articles, countries   M2M
    article_categories   -> raw_articles, category_defs  M2M
    article_translations -> raw_articles, languages   M2M

Purely additive. Nothing existing is altered or dropped, so the current
pipeline keeps running throughout.

As with 001, every column is spelled out rather than derived from
``Base.metadata`` — a migration is a frozen snapshot of history, not a
reflection of whatever the ORM happens to look like today.

`requires_context` and `keywords` are JSON-encoded Text rather than
PostgreSQL ARRAY so the same DDL runs against SQLite in the test suite. The
loss is native array operators, which the classifier does not use — it reads
these once at startup and matches in Python.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "003_phase1_schema"
down_revision = "002_add_url_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- geography --------------------------------------------------------
    op.create_table(
        "regions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    )

    op.create_table(
        "countries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("iso2", sa.String(2), nullable=False, unique=True),
        sa.Column("iso3", sa.String(3), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("region_id", sa.Integer, sa.ForeignKey("regions.id"), nullable=True),
        sa.Column("default_language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("flag_emoji", sa.String(16), nullable=True),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cached_article_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cached_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_countries_is_enabled", "countries", ["is_enabled"])
    op.create_index("ix_countries_region_id", "countries", ["region_id"])

    op.create_table(
        "country_aliases",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("country_id", sa.Integer, sa.ForeignKey("countries.id"), nullable=False),
        sa.Column("alias", sa.String(200), nullable=False),
        sa.Column("alias_type", sa.String(20), nullable=False, server_default="name"),
        sa.Column("weight", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("is_ambiguous", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("requires_context", sa.Text, nullable=True),
    )
    # The gazetteer lookup: lowercased alias is the hot path in §8 Tier 2.
    op.create_index("ix_country_aliases_alias", "country_aliases", ["alias"])
    op.create_index("ix_country_aliases_country_id", "country_aliases", ["country_id"])

    # ---- taxonomy ---------------------------------------------------------
    op.create_table(
        "category_defs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("category_defs.id"), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("keywords", sa.Text, nullable=True),
        sa.Column("color_token", sa.String(50), nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_category_defs_parent_id", "category_defs", ["parent_id"])

    # ---- languages --------------------------------------------------------
    op.create_table(
        "languages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("native_name", sa.String(100), nullable=False),
        sa.Column("direction", sa.String(3), nullable=False, server_default="ltr"),
        sa.Column("translation_tier", sa.String(10), nullable=False, server_default="argos"),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    )

    # ---- permissions ------------------------------------------------------
    # Every default here is restrictive on purpose (V2 §3.6). A source added
    # without review may contribute a headline, a short excerpt and a link.
    op.create_table(
        "source_permissions",
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id"), primary_key=True),
        sa.Column("can_store_title", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("can_store_description", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("can_store_full_text", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("can_store_image", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("can_generate_summary", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("can_translate", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("requires_attribution", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("original_url_required", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("max_description_chars", sa.Integer, nullable=False, server_default="300"),
        sa.Column("robots_override", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("terms_url", sa.Text, nullable=True),
        sa.Column("robots_url", sa.Text, nullable=True),
        sa.Column("licence_note", sa.Text, nullable=True),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime, nullable=True),
        sa.Column("review_notes", sa.Text, nullable=True),
    )
    # Give every existing source a restrictive row so nothing is unprotected.
    op.execute(
        "INSERT INTO source_permissions (source_id) SELECT id FROM sources"
    )

    # ---- article associations --------------------------------------------
    op.create_table(
        "article_countries",
        sa.Column("article_id", sa.Integer, sa.ForeignKey("raw_articles.id"), primary_key=True),
        sa.Column("country_id", sa.Integer, sa.ForeignKey("countries.id"), primary_key=True),
        sa.Column("relevance", sa.String(12), nullable=False, server_default="primary"),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("method", sa.String(12), nullable=False, server_default="gazetteer"),
        # DENORMALISED from raw_articles — see V2 §3.4. This is what turns the
        # hottest query in the product into an index-only range scan.
        sa.Column("published_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "ix_article_countries_feed",
        "article_countries",
        ["country_id", "relevance", sa.text("published_at DESC")],
    )

    op.create_table(
        "article_categories",
        sa.Column("article_id", sa.Integer, sa.ForeignKey("raw_articles.id"), primary_key=True),
        sa.Column("category_id", sa.Integer, sa.ForeignKey("category_defs.id"), primary_key=True),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("method", sa.String(12), nullable=False, server_default="rule"),
    )
    op.create_index(
        "ix_article_categories_category", "article_categories", ["category_id", "article_id"]
    )

    op.create_table(
        "article_translations",
        sa.Column("article_id", sa.Integer, sa.ForeignKey("raw_articles.id"), primary_key=True),
        sa.Column("language_id", sa.Integer, sa.ForeignKey("languages.id"), primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("method", sa.String(12), nullable=False, server_default="argos"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_article_translations_lang", "article_translations", ["language_id", "article_id"]
    )


def downgrade() -> None:
    # Reverse dependency order.
    op.drop_table("article_translations")
    op.drop_table("article_categories")
    op.drop_table("article_countries")
    op.drop_table("source_permissions")
    op.drop_table("languages")
    op.drop_table("category_defs")
    op.drop_table("country_aliases")
    op.drop_table("countries")
    op.drop_table("regions")
