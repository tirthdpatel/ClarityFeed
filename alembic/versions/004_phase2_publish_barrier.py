"""Phase 2 — publish barrier, ops tables, pgvector.

Revision ID: 004_phase2_publish_barrier
Revises: 003_phase1_schema
Create Date: 2026-08-19

Three changes, all from ARCHITECTURE_V2:

**§A3 — the publish barrier.** One `status` column conflated "is this article
ready to show a reader?" with "has the LLM finished with it?". That coupling
is why an LLM outage emptied the site: articles were ingested, classified and
clustered, then sat at PENDING forever because only the categoriser set
PROCESSED. Splitting the column means enrichment can fail all day and readers
still get news.

    ingest_status      PENDING -> FETCHED -> CLEANED -> PUBLISHED | FAILED
    enrichment_status  PENDING -> DONE | FAILED | SKIPPED

`status` is kept as a synced shadow column for one release so the existing
pipeline keeps running, and is dropped in a later revision once the new
columns are verified in production. Per V2 Part C: never drop in the same
revision that adds the replacement.

**§A8 — ops tables.** `ingestion_runs` and `source_health`. The column that
matters is `articles_returned`: a feed that answers HTTP 200 with zero
entries for six consecutive runs is dead, and keying auto-disable off HTTP
status alone would never notice.

**§A5 — pgvector.** The extension is free on Supabase (V2 assumed it was paid
on Neon and stored 384 floats as JSON text instead — 4.5x larger and forcing
similarity into application RAM). `halfvec(384)` plus HNSW moves search into
the database. `vector_json` is backfilled and left in place; it is dropped in
a later revision after verification.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004_phase2_publish_barrier"
down_revision = "003_phase1_schema"
branch_labels = None
depends_on = None

INGEST_STATES = ("PENDING", "FETCHED", "CLEANED", "PUBLISHED", "FAILED")
ENRICH_STATES = ("PENDING", "DONE", "FAILED", "SKIPPED")


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ---- 1. publish barrier -------------------------------------------
    ingest_enum = sa.Enum(*INGEST_STATES, name="ingest_status")
    enrich_enum = sa.Enum(*ENRICH_STATES, name="enrichment_status")
    if is_pg:
        ingest_enum.create(bind, checkfirst=True)
        enrich_enum.create(bind, checkfirst=True)

    op.add_column("raw_articles", sa.Column("ingest_status", ingest_enum, nullable=True))
    op.add_column("raw_articles", sa.Column("enrichment_status", enrich_enum, nullable=True))

    # Backfill from the old column. PROCESSED meant "the categoriser finished",
    # which under the old design was the only thing that ever made an article
    # visible — so it maps to PUBLISHED.
    #
    # PostgreSQL will not implicitly assign text to an enum column inside a
    # CASE, so the branches are cast explicitly. SQLite has no enum type and
    # no `::` cast syntax, hence the split.
    if is_pg:
        op.execute("""
            UPDATE raw_articles SET
                ingest_status = (CASE
                    WHEN status::text = 'PROCESSED' THEN 'PUBLISHED'
                    WHEN status::text = 'FAILED'    THEN 'FAILED'
                    ELSE 'PENDING' END)::ingest_status,
                enrichment_status = (CASE
                    WHEN status::text = 'PROCESSED' THEN 'DONE'
                    ELSE 'PENDING' END)::enrichment_status
        """)
    else:
        op.execute("""
            UPDATE raw_articles SET
                ingest_status = CASE
                    WHEN status = 'PROCESSED' THEN 'PUBLISHED'
                    WHEN status = 'FAILED'    THEN 'FAILED'
                    ELSE 'PENDING' END,
                enrichment_status = CASE
                    WHEN status = 'PROCESSED' THEN 'DONE'
                    ELSE 'PENDING' END
        """)

    op.alter_column("raw_articles", "ingest_status", nullable=False)
    op.alter_column("raw_articles", "enrichment_status", nullable=False)

    # The hot path: "latest published articles". Without this the homepage
    # sequential-scans raw_articles on every ISR regeneration.
    op.create_index(
        "ix_raw_articles_published",
        "raw_articles",
        ["ingest_status", sa.text("published_at DESC")],
    )
    op.create_index(
        "ix_raw_articles_enrichment", "raw_articles", ["enrichment_status"]
    )

    # ---- 2. ops tables -------------------------------------------------
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column("trigger", sa.String(12), nullable=False, server_default="cron"),
        sa.Column("status", sa.String(12), nullable=False, server_default="running"),
        sa.Column("sources_attempted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sources_ok", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sources_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("articles_new", sa.Integer, nullable=False, server_default="0"),
        sa.Column("articles_duplicate", sa.Integer, nullable=False, server_default="0"),
        sa.Column("articles_published", sa.Integer, nullable=False, server_default="0"),
        sa.Column("clusters_created", sa.Integer, nullable=False, server_default="0"),
        sa.Column("llm_calls_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text, nullable=True),
        sa.Column("git_sha", sa.String(40), nullable=True),
    )
    op.create_index("ix_ingestion_runs_started", "ingestion_runs", [sa.text("started_at DESC")])

    op.create_table(
        "source_health",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("checked_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("ok", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("http_status", sa.Integer, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        # A8: this column, not http_status, is what catches a dead feed.
        sa.Column("articles_returned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(50), nullable=True),
        sa.Column("error_detail", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_source_health_source", "source_health",
        ["source_id", sa.text("checked_at DESC")],
    )

    # Denormalised failure counters so the admin dashboard does not aggregate
    # source_health on every page load.
    op.add_column("sources", sa.Column("last_success_at", sa.DateTime, nullable=True))
    op.add_column("sources", sa.Column("last_error_at", sa.DateTime, nullable=True))
    op.add_column("sources", sa.Column("last_error", sa.Text, nullable=True))
    op.add_column("sources", sa.Column("consecutive_failures", sa.Integer,
                                       nullable=False, server_default="0"))
    op.add_column("sources", sa.Column("editorial_weight", sa.Float,
                                       nullable=False, server_default="1.0"))

    # ---- 3. pgvector ---------------------------------------------------
    if is_pg:
        # Supabase ships pgvector (confirmed available, v0.8.2). A stock
        # PostgreSQL without the extension package installed does not, and a
        # developer running migrations against a plain local Postgres should
        # not be blocked by it — the vector column is only needed by the
        # ingestion pipeline, never by the API. So: check, then warn and skip.
        available = bind.execute(
            sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        ).scalar()

        if available:
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")

            # halfvec stores 2 bytes per dimension instead of 4: 768 bytes at
            # 384 dims versus 1536, against ~3.5 KB for the JSON-text form it
            # replaces (the 4.5x waste in §A5). It requires pgvector >= 0.7.
            # Supabase ships 0.8.2; Debian's package is still 0.6.0, so the
            # type genuinely may not exist and `vector` is the fallback. Both
            # are enormous improvements over JSON text, so degrading is fine.
            has_halfvec = bind.execute(
                sa.text("SELECT 1 FROM pg_type WHERE typname = 'halfvec'")
            ).scalar()
            coltype = "halfvec(384)" if has_halfvec else "vector(384)"
            op.execute(f"ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS vector {coltype}")
            if not has_halfvec:
                import warnings
                warnings.warn(
                    "pgvector < 0.7 — using vector(384) instead of halfvec(384). "
                    "Twice the storage per embedding, otherwise identical.",
                    stacklevel=2,
                )
            # The HNSW index is built in a later revision, once rows exist.
            # Building it over an empty table is pointless and it would have
            # to be rebuilt after the first real backfill anyway.
        else:
            import warnings
            warnings.warn(
                "pgvector is not available on this server — skipping the "
                "embeddings.vector column. Clustering will fall back to the "
                "vector_json path. Install postgresql-<ver>-pgvector, or run "
                "against Supabase, then re-run this revision.",
                stacklevel=2,
            )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("ALTER TABLE embeddings DROP COLUMN IF EXISTS vector")

    for col in ("editorial_weight", "consecutive_failures", "last_error",
                "last_error_at", "last_success_at"):
        op.drop_column("sources", col)

    op.drop_index("ix_source_health_source", table_name="source_health")
    op.drop_table("source_health")
    op.drop_index("ix_ingestion_runs_started", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")

    op.drop_index("ix_raw_articles_enrichment", table_name="raw_articles")
    op.drop_index("ix_raw_articles_published", table_name="raw_articles")
    op.drop_column("raw_articles", "enrichment_status")
    op.drop_column("raw_articles", "ingest_status")

    if is_pg:
        sa.Enum(name="enrichment_status").drop(bind, checkfirst=True)
        sa.Enum(name="ingest_status").drop(bind, checkfirst=True)
