"""Distinguish how a source is collected, and let publishers be discovered.

Revision ID: 006_source_kind
Revises: 005_takedown_column
Create Date: 2026-09-09

Every source until now was an RSS feed, so the runner could assume one
collection method and `feed_url` was always a real URL. Adding the GNews API
breaks both assumptions.

`kind` says how to collect a source. 'rss' is the default and describes every
existing row, so this migration changes no behaviour on its own.

The harder problem is attribution. GNews returns articles from dozens of
publishers under one API key, and this schema ties an article to exactly one
source row — which is also where permissions hang. Filing all of them under a
single "GNews" row would credit GNews for other people's journalism and give
fifty publishers one shared permission row, which defeats the point of having
the permission model at all.

So publishers found through an aggregator get their own `sources` row,
created on demand, with kind='discovered'. They are `is_active = false`
because there is no feed to poll — they exist to own an attribution and a
permission row. Being unreviewed, they resolve to Permissions.restrictive()
like any other unreviewed source, which is the correct default for a
publisher nobody has looked at.

`feed_url` is NOT NULL and unique, and a discovered publisher has no feed, so
it gets a synthetic `gnews://<slug>`. Deliberately not http(s): nothing should
ever try to fetch it, and a scheme that no fetcher understands makes that
failure loud rather than silent.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006_source_kind"
down_revision = "005_takedown_column"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "kind",
            sa.String(20),
            nullable=False,
            server_default="rss",
            comment="How this source is collected: rss, gnews, or discovered "
            "(a publisher found through an aggregator; has no feed of its own).",
        ),
    )
    # Partial index: the runner asks "which sources do I poll?" on every cycle
    # and discovered publishers are the majority of rows once an aggregator has
    # been running. Indexing only the pollable kinds keeps that lookup small.
    op.create_index(
        "ix_sources_kind_active",
        "sources",
        ["kind", "is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_sources_kind_active", table_name="sources")
    op.drop_column("sources", "kind")
