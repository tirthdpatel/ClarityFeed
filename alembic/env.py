"""Alembic migration environment for ClarityFeed.

The database URL comes from ``config.settings`` (and therefore from the
``DATABASE_URL`` environment variable), never from ``alembic.ini``. That
keeps the connection string out of version control and means migrations run
against whatever the application itself is pointed at — including the
Supabase move in Phase 0.
"""
from __future__ import annotations

import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from backend.database.orm_models import Base
from config.settings import settings

config = context.config

# NOTE: do NOT do this:
#
#     config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
#
# alembic.ini is parsed by configparser with interpolation enabled, so a '%'
# in the value is treated as interpolation syntax. Any URL-encoded character
# in the password — '%40' for '@', '%23' for '#' — raises:
#
#     ValueError: invalid interpolation syntax in '...' at position N
#
# Escaping to '%%' would work but is easy to forget and silently wrong if the
# URL is ever read back. Bypassing the ini file entirely is simpler and has no
# failure mode: the engine is constructed directly from settings below.

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _masked_url() -> str:
    """The connection URL with the password replaced by '***'.

    Used in error messages so a failure never reveals the credential.
    """
    return re.sub(
        r"^(\w+://[^:/@]*:)[^@]*(@)", r"\1***\2", settings.DATABASE_URL
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade --sql``)."""
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection.

    The engine is built straight from ``settings.DATABASE_URL`` rather than
    from the ini section, so the URL never passes through configparser and
    percent-encoded passwords work unmodified. See the note at the top.
    """
    # Build the engine inside a guard that strips the password from any error.
    # The configparser bug this file used to have raised a ValueError whose
    # message contained the ENTIRE connection string, so the password landed
    # in a terminal scrollback and anywhere that traceback was pasted.
    # SQLAlchemy masks passwords in its own URL repr; this makes sure anything
    # raised on the way to SQLAlchemy does too.
    try:
        connectable = create_engine(settings.DATABASE_URL, poolclass=pool.NullPool)
    except Exception as exc:
        raise RuntimeError(
            f"Could not create the database engine from DATABASE_URL "
            f"({_masked_url()}): {type(exc).__name__}. "
            f"Run 'python scripts/check_db.py' for a detailed diagnosis."
        ) from None

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # Required for ALTER on SQLite, which the test suite uses.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
