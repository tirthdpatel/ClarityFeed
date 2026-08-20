"""Baseline — the 6-table schema as it existed before Alembic was introduced.

Revision ID: 001_baseline
Revises:
Create Date: 2026-08-19

This revision defines the ORIGINAL schema explicitly, column by column.

Why not ``Base.metadata.create_all()``
--------------------------------------
That was the first implementation and it was wrong. ``Base.metadata`` reflects
the ORM *as it is today*, which already contains ``url_hash`` — so on a fresh
database this revision created ``url_hash``, and then revision 002 tried to
add it again and failed::

    psycopg2.errors.DuplicateColumn: column "url_hash" of relation
    "raw_articles" already exists

The bug only appears on an empty database. Migrating an existing deployment
worked fine, which is exactly the kind of asymmetry that reaches production.

The rule it violated: **a migration is a frozen snapshot of history, never
derived from live models.** Models keep changing; migration 001 must always
describe the schema of 9 August 2026 no matter what the ORM looks like in a
year. Anything else means the sequence stops replaying correctly, and a
replayable sequence is the entire point of having migrations.

Usage
-----
Fresh database::

    alembic upgrade head          # creates the baseline, then applies 002+

Existing deployment (tables already created by the old create_all path)::

    alembic stamp 001_baseline    # adopt without re-creating
    alembic upgrade head
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "001_baseline"
down_revision = None
branch_labels = None
depends_on = None

_BASELINE_TABLES = (
    "sources",
    "raw_articles",
    "cleaned_articles",
    "embeddings",
    "summaries",
    "categories",
)


def _already_present(bind) -> bool:
    return bool(set(sa.inspect(bind).get_table_names()) & set(_BASELINE_TABLES))


def upgrade() -> None:
    bind = op.get_bind()

    # Existing deployment: these tables were created by the pre-Alembic
    # create_all() path. Adopt them rather than fail.
    if _already_present(bind):
        return

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("feed_url", sa.String(2048), nullable=False, unique=True),
        sa.Column("language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("country", sa.String(100), nullable=False, server_default=""),
        sa.Column("category", sa.String(100), nullable=False, server_default="general"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    pipeline_status = sa.Enum(
        "PENDING", "FETCHED", "CLEANED", "PROCESSED", "FAILED",
        name="pipeline_status",
    )

    op.create_table(
        "raw_articles",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id"), nullable=False, index=True),
        # NOTE: unique=True on a 2048-char column is the A4 defect. It is
        # reproduced faithfully here because this revision documents history;
        # revision 002 is what fixes it.
        sa.Column("url", sa.String(2048), nullable=False, unique=True),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("published_at", sa.DateTime, nullable=True),
        sa.Column("raw_html", sa.Text, nullable=True),
        sa.Column("summary_from_feed", sa.Text, nullable=True),
        sa.Column("status", pipeline_status, nullable=False, index=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "cleaned_articles",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("raw_article_id", sa.Integer, sa.ForeignKey("raw_articles.id"), nullable=False, unique=True),
        sa.Column("clean_text", sa.Text, nullable=False),
        sa.Column("word_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "embeddings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("raw_article_id", sa.Integer, sa.ForeignKey("raw_articles.id"), nullable=False, unique=True),
        sa.Column("vector_json", sa.Text, nullable=True),
        sa.Column("model_name", sa.String(255), nullable=False,
                  server_default="sentence-transformers/all-MiniLM-L6-v2"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "summaries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("raw_article_id", sa.Integer, sa.ForeignKey("raw_articles.id"), nullable=False, unique=True),
        sa.Column("summary_text", sa.Text, nullable=False),
        sa.Column("tldr", sa.Text, nullable=True),
        sa.Column("bullet_points", sa.Text, nullable=True),
        sa.Column("model_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "categories",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("raw_article_id", sa.Integer, sa.ForeignKey("raw_articles.id"), nullable=False, unique=True),
        sa.Column("primary_category", sa.String(255), nullable=False, server_default=""),
        sa.Column("secondary_category", sa.String(255), nullable=True),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("model_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    # Deliberately not implemented. Downgrading past the baseline drops every
    # table in the application, which no migration should do by accident.
    raise NotImplementedError(
        "Cannot downgrade past the baseline revision — this would drop all tables."
    )
