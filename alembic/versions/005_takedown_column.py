"""Record publisher takedown requests as state.

Revision ID: 005_takedown_column
Revises: 004_phase2_publish_barrier
Create Date: 2026-09-08

ARCHITECTURE_V2 §11 specifies a documented takedown path and a column to
record it; the process existed on paper and the column never got added, so
"we removed this publisher on request" lived only in someone's memory and in
`is_active = false`, which is indistinguishable from "this feed kept 502ing
and the circuit breaker disabled it".

That distinction is the entire point. A source disabled for being broken
should be retried when it recovers; a source disabled because its publisher
asked must never come back on its own. With one boolean carrying both
meanings, the only thing standing between a takedown and its own reversal is
whoever next looks at a list of inactive feeds and decides to re-enable the
ones that look healthy again.

Three columns rather than one, because a takedown has three separately useful
facts: when it was asked for, who asked, and what they said. `requested_by` is
free text on purpose — it is a person and an email address in practice, not an
entity worth modelling.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "005_takedown_column"
down_revision = "004_phase2_publish_barrier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "takedown_requested_at",
            sa.DateTime(),
            nullable=True,
            comment="Set when a publisher asks to be removed. NULL means no "
            "request. Enforced in get_active_sources() and in the read API.",
        ),
    )
    op.add_column(
        "sources",
        sa.Column("takedown_requested_by", sa.String(255), nullable=True),
    )
    op.add_column("sources", sa.Column("takedown_note", sa.Text(), nullable=True))

    # Partial index: the query this serves is "which sources are under
    # takedown?", which is a handful of rows against a table that is otherwise
    # scanned whole. Indexing the NULLs would be pure write cost.
    op.create_index(
        "ix_sources_takedown_requested_at",
        "sources",
        ["takedown_requested_at"],
        unique=False,
        postgresql_where=sa.text("takedown_requested_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sources_takedown_requested_at", table_name="sources")
    op.drop_column("sources", "takedown_note")
    op.drop_column("sources", "takedown_requested_by")
    op.drop_column("sources", "takedown_requested_at")
