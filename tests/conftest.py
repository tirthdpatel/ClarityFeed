"""Shared pytest configuration.

Why this file exists
--------------------
``config.settings.settings`` is a module-level singleton, built the first time
anything imports ``config.settings``. Individual test modules were setting
environment variables at the top of the file and then importing application
code, which works when that module runs alone and silently fails in a full
suite: an earlier module already triggered the import, so the singleton was
built from defaults and the later assignment had no effect.

pytest imports conftest.py before any test module, so this is the only place
that reliably wins.

WHY A TEMP FILE AND NOT ``:memory:``
------------------------------------
``sqlite:///:memory:`` gives every *connection* its own empty database. The
app opens a new session per request, so a table created during fixture setup
is invisible to the request under test::

    sqlite3.OperationalError: no such table: sources

This only surfaced when the suite ran with no ``DATABASE_URL`` already
exported — running with an explicit file URL masked it completely, which is
why it passed in one environment and failed in another. A temp file behaves
like a real database: one shared store, visible across connections.
"""
from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "clarityfeed_test.db"
_TMP_DB.unlink(missing_ok=True)

# Must run before any `from config.settings import settings` anywhere.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DB}")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("HF_API_TOKEN", "test_hf_token")
os.environ.setdefault("FRONTEND_URL", "http://localhost:3000")
os.environ.setdefault("APP_ENV", "development")

# The rate limiter keeps its counters in process memory, so every request the
# suite makes shares one window. At the production setting of 60/minute the
# tests throttle themselves: whichever test happens to run past the sixtieth
# request starts getting 429s, which makes failures depend on test ORDER
# rather than on behaviour — and gets worse with every test added.
#
# Raised rather than disabled, so the middleware still runs in the path it
# would run in production. The limiter's own behaviour is covered directly in
# tests/unit/test_rate_limit.py, which builds an instance with a small window.
os.environ.setdefault("RATE_LIMIT_REQUESTS", "1000000")


@atexit.register
def _cleanup() -> None:
    _TMP_DB.unlink(missing_ok=True)
