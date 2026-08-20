#!/usr/bin/env python3
"""Verify the DATABASE_URL in .env actually works.

    python scripts/check_db.py

Prints a diagnosis and exits 0 on success, 1 on failure. The password is
never printed — only its length and whether it needs URL-encoding.

Checks, in order (cheap and offline first, so an obvious typo is reported
in milliseconds instead of after a 30-second timeout):

  1. .env exists and defines DATABASE_URL
  2. No placeholder text remains
  3. Supabase connection mode is the right one for this project
  4. The password is safe to embed in a URL
  5. DNS resolves
  6. TCP connects
  7. Postgres authenticates
  8. Server version, current database, pgvector availability, Alembic state
"""
from __future__ import annotations

import re
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

RED, GREEN, YELLOW, BOLD, DIM, RESET = (
    "\033[31m", "\033[32m", "\033[33m", "\033[1m", "\033[2m", "\033[0m"
)

def ok(m: str) -> None:    print(f"  {GREEN}OK{RESET}    {m}")
def warn(m: str) -> None:  print(f"  {YELLOW}WARN{RESET}  {m}")
def fail(m: str) -> None:  print(f"  {RED}FAIL{RESET}  {m}")


def read_database_url() -> str | None:
    if not ENV_PATH.exists():
        fail(f".env not found at {ENV_PATH}")
        print(f"\n  Create it:  cp .env.example .env\n")
        return None
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    fail("No non-empty DATABASE_URL line in .env")
    return None


def main() -> int:
    print(f"\n{BOLD}ClarityFeed database check{RESET}\n")

    url = read_database_url()
    if url is None:
        return 1

    # -- Structure ---------------------------------------------------------
    m = re.match(r"^(postgres(?:ql)?)://([^:/@]*):(.*)@([^:/]+):(\d+)/(.+)$", url)
    if not m:
        fail("DATABASE_URL is not in the expected form")
        print(f"\n  Expected:\n    postgresql://USER:PASSWORD@HOST:PORT/DATABASE\n")
        return 1

    scheme, user, password, host, port_s, dbname = m.groups()
    port = int(port_s)
    print(f"  {DIM}{scheme}://{user}:{'#' * len(password)}@{host}:{port}/{dbname}{RESET}\n")

    problems = 0

    # -- Placeholders ------------------------------------------------------
    leftover = [p for p in ("[YOUR-PASSWORD]", "[REGION]", "[PROJECT-REF]") if p in url]
    if leftover:
        fail(f"Placeholder text still present: {', '.join(leftover)}")
        print("\n  The template was not fully replaced. Open .env and replace the")
        print("  entire DATABASE_URL line with the string copied from:")
        print("    Supabase dashboard -> Connect -> Session pooler\n")
        return 1
    ok("no placeholders remain")

    # -- Supabase connection mode -----------------------------------------
    if "pooler.supabase.com" in host:
        if port == 5432:
            ok("session pooler on 5432 — correct (IPv4, supports migrations)")
        elif port == 6543:
            fail("port 6543 is the TRANSACTION pooler")
            print("\n  Transaction mode holds no session state and disallows prepared")
            print("  statements, which breaks Alembic. Change the port to 5432.\n")
            problems += 1
        if not user.startswith("postgres."):
            fail(f"pooler requires username 'postgres.<project-ref>', got '{user}'")
            problems += 1
        else:
            ok(f"pooler username format correct ({user})")
    elif host.startswith("db.") and host.endswith(".supabase.co"):
        warn("this is the DIRECT connection (IPv6-only on the Supabase free tier)")
        print(f"        {DIM}It may work from your Mac but will FAIL from GitHub Actions,{RESET}")
        print(f"        {DIM}which is IPv4-only and runs the whole ingestion pipeline.{RESET}")
        print(f"        {DIM}Prefer the session pooler string.{RESET}")
    else:
        warn(f"unrecognised host pattern: {host}")

    # -- The bracket trap --------------------------------------------------
    # The Supabase dashboard renders the placeholder as [YOUR-PASSWORD].
    # Replacing only the text *inside* the brackets is the single most common
    # way this goes wrong, and the resulting error ("Tenant or user not found"
    # or a plain auth failure) points nowhere near the actual cause.
    if password.startswith("[") and password.endswith("]"):
        fail("password is wrapped in square brackets")
        print("\n  The [ ] are placeholder syntax from the dashboard, not part of")
        print("  your password. Remove them:")
        print(f"    [{'*' * (len(password) - 2)}]  ->  {'*' * (len(password) - 2)}\n")
        return 1

    # -- Password encoding -------------------------------------------------
    unsafe = set(re.findall(r"[@:/?#\[\]]", password))
    if unsafe:
        fail(f"password contains characters that must be URL-encoded: {' '.join(sorted(unsafe))}")
        print("\n  These break URL parsing and produce a confusing auth error.")
        print("  Encode them:  @ -> %40   : -> %3A   / -> %2F   # -> %23")
        print("  Or reset to an alphanumeric password in the Supabase dashboard.\n")
        problems += 1
    else:
        ok(f"password is URL-safe ({len(password)} characters)")

    if problems:
        print(f"\n{RED}{BOLD}{problems} problem(s) found — not attempting to connect.{RESET}\n")
        return 1

    # -- DNS ---------------------------------------------------------------
    print()
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        families = {i[0] for i in infos}
        has_v4 = socket.AF_INET in families
        has_v6 = socket.AF_INET6 in families
        ok(f"DNS resolves ({'IPv4' if has_v4 else ''}{' + ' if has_v4 and has_v6 else ''}{'IPv6' if has_v6 else ''})")
        if has_v6 and not has_v4:
            warn("IPv6 only — this will not work from GitHub Actions")
    except socket.gaierror as e:
        fail(f"DNS lookup failed for {host}: {e}")
        print("\n  Check the region in the hostname matches your project's region.\n")
        return 1

    # -- TCP ---------------------------------------------------------------
    try:
        with socket.create_connection((host, port), timeout=10):
            ok(f"TCP connect to {host}:{port}")
    except OSError as e:
        fail(f"cannot reach {host}:{port} — {e}")
        return 1

    # -- Authenticate ------------------------------------------------------
    try:
        import psycopg2
    except ImportError:
        warn("psycopg2 not installed — cannot verify authentication")
        print("\n  pip install -r requirements.txt\n")
        return 1

    try:
        conn = psycopg2.connect(url, connect_timeout=15)
    except psycopg2.OperationalError as e:
        msg = str(e).strip()
        low = msg.lower()

        # Supabase shards poolers as aws-0-<region> / aws-1-<region>, and the
        # dashboard only ever shows yours. Picking the wrong shard yields
        # "Tenant or user not found", which sounds like a credential problem
        # and is not. Try the sibling before reporting failure.
        if "tenant or user not found" in low and re.match(r"^aws-[01]-", host):
            sibling = ("aws-1-" if host.startswith("aws-0-") else "aws-0-") + host.split("-", 2)[2]
            warn(f"{host} rejected the tenant — trying {sibling}")
            alt = url.replace(host, sibling)
            try:
                conn = psycopg2.connect(alt, connect_timeout=15)
                ok(f"connected via {sibling}")
                print(f"\n  {YELLOW}Update .env — the pooler host should be:{RESET}")
                print(f"    {BOLD}{sibling}{RESET}\n")
                host = sibling
            except psycopg2.OperationalError as e2:
                fail("both pooler shards rejected the connection")
                print(f"\n  {DIM}{str(e2).strip()}{RESET}\n")
                return 1
        else:
            fail("Postgres refused the connection")
            print(f"\n  {DIM}{msg}{RESET}\n")
            if "password authentication failed" in low:
                print("  Wrong password, or the wrong username for this mode.")
                print("  Pooler needs 'postgres.<project-ref>', direct needs plain 'postgres'.")
                print("  If the password came from the dashboard, check you did not keep")
                print("  the surrounding [ ] — they are placeholder syntax, not characters.")
                print("  Reset it: Supabase -> Settings -> Database -> Reset password")
            elif "tenant or user not found" in low:
                print("  The project ref in the username does not match this pooler host.")
                print("  Re-copy the whole string from the dashboard.")
            elif "timeout" in low or "could not connect" in low:
                print("  Likely an IPv6-only direct connection, or the project is paused.")
                print("  Supabase pauses free projects after ~7 days idle — check the dashboard.")
            print()
            return 1

    # -- Report ------------------------------------------------------------
    with conn, conn.cursor() as cur:
        cur.execute("SELECT version(), current_database(), current_user")
        version, current_db, current_user = cur.fetchone()
        ok("authenticated")
        print(f"\n  {BOLD}Server{RESET}   {version.split(',')[0]}")
        print(f"  {BOLD}Database{RESET} {current_db}")
        print(f"  {BOLD}User{RESET}     {current_user}")

        cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        available = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        installed = cur.fetchone() is not None
        if installed:
            print(f"  {BOLD}pgvector{RESET} installed")
        elif available:
            print(f"  {BOLD}pgvector{RESET} available, not yet enabled {DIM}(Phase 2 migration enables it){RESET}")
        else:
            print(f"  {BOLD}pgvector{RESET} {YELLOW}not available{RESET}")

        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' ORDER BY table_name
        """)
        tables = [r[0] for r in cur.fetchall()]
        print(f"  {BOLD}Tables{RESET}   {len(tables)}" + (f" {DIM}({', '.join(tables[:8])}{'...' if len(tables) > 8 else ''}){RESET}" if tables else f" {DIM}(empty — migrations not yet run){RESET}"))

        if "alembic_version" in tables:
            cur.execute("SELECT version_num FROM alembic_version")
            row = cur.fetchone()
            print(f"  {BOLD}Alembic{RESET}  {row[0] if row else 'no revision stamped'}")
        else:
            print(f"  {BOLD}Alembic{RESET}  {DIM}not initialised — run: alembic upgrade head{RESET}")

    conn.close()
    print(f"\n{GREEN}{BOLD}Connection works.{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
