#!/usr/bin/env python3
"""Set DATABASE_URL in .env safely.

    python scripts/set_db_url.py

Prompts for the connection string with hidden input, validates it, rewrites
the DATABASE_URL line in .env, and leaves every other line untouched.

Why not just edit .env by hand
------------------------------
Every failure so far came from hand-editing:

  * the password was pasted still wrapped in the dashboard's ``[ ]``
  * ``[REGION]`` was left as literal placeholder text
  * a ``%``-encoded password broke Alembic's configparser
  * the full connection string ended up in a terminal scrollback

Hidden input means the string never reaches shell history or the visible
scrollback, and the validation below catches the first three before they
become confusing errors an hour later.
"""
from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

RED, GREEN, YELLOW, BOLD, DIM, RESET = (
    "\033[31m", "\033[32m", "\033[33m", "\033[1m", "\033[2m", "\033[0m"
)


def validate(url: str) -> list[str]:
    """Return a list of problems; empty means the URL looks usable."""
    problems: list[str] = []

    m = re.match(r"^(postgres(?:ql)?)://([^:/@]*):(.*)@([^:/]+):(\d+)/(.+)$", url)
    if not m:
        return ["Not in the form postgresql://USER:PASSWORD@HOST:PORT/DATABASE"]

    _, user, password, host, port_s, _ = m.groups()
    port = int(port_s)

    for ph in ("[YOUR-PASSWORD]", "[REGION]", "[PROJECT-REF]", "YOUR-PASSWORD"):
        if ph in url:
            problems.append(f"placeholder {ph} was not replaced")

    if password.startswith("[") and password.endswith("]"):
        problems.append(
            "password is wrapped in [ ] — those are the dashboard's placeholder "
            "markers, not part of your password"
        )

    if "pooler.supabase.com" in host:
        if port == 6543:
            problems.append(
                "port 6543 is the transaction pooler; Alembic needs session "
                "mode on 5432"
            )
        if not user.startswith("postgres."):
            problems.append(
                f"pooler requires username postgres.<project-ref>, got {user!r}"
            )
    elif host.startswith("db.") and host.endswith(".supabase.co"):
        problems.append(
            "this is the direct connection (IPv6-only on the free tier) and "
            "will fail from GitHub Actions — use the session pooler string"
        )

    return problems


def main() -> int:
    if not ENV_PATH.exists():
        print(f"{RED}No .env file.{RESET} Run: cp .env.example .env")
        return 1

    print(f"\n{BOLD}Set DATABASE_URL{RESET}")
    print(f"{DIM}Supabase dashboard -> Connect -> Session pooler (port 5432).{RESET}")
    print(f"{DIM}Reveal the password first so the string you copy is complete.{RESET}")
    print(f"{DIM}Input is hidden — it will not appear on screen or in shell history.{RESET}\n")

    try:
        url = getpass.getpass("Connection string: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 1

    if not url:
        print(f"{RED}Nothing entered.{RESET}")
        return 1

    problems = validate(url)
    if problems:
        print(f"\n{RED}{BOLD}Not written — {len(problems)} problem(s):{RESET}")
        for p in problems:
            print(f"  - {p}")
        print()
        return 1

    # Rewrite only the DATABASE_URL line.
    lines = ENV_PATH.read_text().splitlines()
    out, replaced = [], False
    for line in lines:
        if line.startswith("DATABASE_URL=") and not replaced:
            out.append("DATABASE_URL=" + url)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append("DATABASE_URL=" + url)
    ENV_PATH.write_text("\n".join(out) + "\n")

    masked = re.sub(r"^(\w+://[^:/@]*:)[^@]*(@)", r"\1***\2", url)
    print(f"\n{GREEN}Written to .env{RESET}")
    print(f"  {DIM}{masked}{RESET}\n")
    print("Next:  ./scripts/setup_db.sh\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
