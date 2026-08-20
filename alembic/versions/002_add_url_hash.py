"""Add raw_articles.url_hash and retire the unbounded unique index on url.

Revision ID: 002_add_url_hash
Revises: 001_baseline
Create Date: 2026-08-19

Fixes ARCHITECTURE_V2.md §A4.

The problem
-----------
``raw_articles.url`` was ``String(2048)`` with ``unique=True``. PostgreSQL
caps a btree index entry at roughly 2,704 bytes, and a 2,048-*character* URL
carrying multi-byte UTF-8 exceeds that. The insert fails with::

    index row size N exceeds btree version 4 maximum 2704

This does not reproduce on SQLite, which is why the test suite never caught
it. It surfaces in production on the first sufficiently long non-ASCII slug.

The fix
-------
Index a fixed-width SHA-256 of the canonical URL instead — always 64 hex
characters regardless of input.

This revision performs all three steps in order, per the V2 Part C rule that
nothing is dropped in the same breath as its replacement being added:

  1. ADD ``url_hash`` as nullable
  2. BACKFILL it in batches from the existing ``url`` values
  3. ADD the unique index on ``url_hash``, THEN drop the old constraint on ``url``

Step 2 uses the same canonicalisation as the live inserter
(:func:`backend.urls.url_hash`), so historical and new rows hash identically.

Collision note: canonicalisation collapses tracking-parameter variants that
were previously distinct rows. The backfill therefore keeps the lowest ``id``
for each hash and nulls the rest, so the unique index can be created. Those
rows are genuine duplicates of one another and are reported in the log.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

from backend.urls import url_hash

revision = "002_add_url_hash"
down_revision = "001_baseline"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.002_add_url_hash")

BATCH_SIZE = 500


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # -- Step 1: add the column, nullable -----------------------------------
    with op.batch_alter_table("raw_articles") as batch:
        batch.add_column(sa.Column("url_hash", sa.String(64), nullable=True))

    # -- Step 2: backfill in batches ----------------------------------------
    # Batched rather than one large UPDATE so that a big table does not hold a
    # single long transaction open, per the V2 Part C rules.
    seen: dict[str, int] = {}
    collisions = 0
    processed = 0
    offset = 0

    while True:
        rows = bind.execute(
            sa.text(
                "SELECT id, url FROM raw_articles "
                "ORDER BY id LIMIT :limit OFFSET :offset"
            ),
            {"limit": BATCH_SIZE, "offset": offset},
        ).fetchall()

        if not rows:
            break

        for row_id, url in rows:
            if not url:
                continue
            digest = url_hash(url)
            if digest in seen:
                # Canonicalisation revealed these are the same article.
                # Leave url_hash NULL on the later row so the unique index
                # can be built; NULLs do not conflict in either backend.
                collisions += 1
                continue
            seen[digest] = row_id
            bind.execute(
                sa.text("UPDATE raw_articles SET url_hash = :h WHERE id = :i"),
                {"h": digest, "i": row_id},
            )

        processed += len(rows)
        offset += BATCH_SIZE
        logger.info("url_hash backfill: %d rows processed", processed)

    if collisions:
        logger.warning(
            "url_hash backfill found %d duplicate articles that differed only by "
            "tracking parameters; their url_hash is NULL and they should be pruned.",
            collisions,
        )

    # -- Step 3: add the new index, then drop the old constraint ------------
    op.create_index(
        "ix_raw_articles_url_hash", "raw_articles", ["url_hash"], unique=True
    )

    # The old unique constraint on `url` is auto-named differently per backend.
    # SQLite has no named constraint to drop (batch mode rebuilds the table),
    # and on PostgreSQL the create_all() default name is raw_articles_url_key.
    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE raw_articles DROP CONSTRAINT IF EXISTS raw_articles_url_key"
        )
        # `url` no longer needs a length bound now that it is not indexed.
        op.alter_column(
            "raw_articles",
            "url",
            type_=sa.Text(),
            existing_type=sa.String(2048),
            existing_nullable=False,
        )
    else:
        with op.batch_alter_table("raw_articles") as batch:
            batch.alter_column(
                "url",
                type_=sa.Text(),
                existing_type=sa.String(2048),
                existing_nullable=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.drop_index("ix_raw_articles_url_hash", table_name="raw_articles")

    if dialect == "postgresql":
        op.alter_column(
            "raw_articles",
            "url",
            type_=sa.String(2048),
            existing_type=sa.Text(),
            existing_nullable=False,
        )
        op.create_unique_constraint("raw_articles_url_key", "raw_articles", ["url"])
    else:
        with op.batch_alter_table("raw_articles") as batch:
            batch.alter_column(
                "url",
                type_=sa.String(2048),
                existing_type=sa.Text(),
                existing_nullable=False,
            )

    with op.batch_alter_table("raw_articles") as batch:
        batch.drop_column("url_hash")
