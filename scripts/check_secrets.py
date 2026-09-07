#!/usr/bin/env python3
"""Block credentials from entering the repository.

Runs in two modes:

    python scripts/check_secrets.py --staged     # pre-commit hook: staged diff only
    python scripts/check_secrets.py              # CI: every tracked file

Exit code 0 = clean, 1 = secrets found, 2 = usage error.

Design notes
------------
This is a *guardrail*, not a security boundary. It catches the realistic
accident — pasting a connection string into a config file, committing a .env
copy, hardcoding an API key while debugging. It cannot catch a determined
person, and it is not a substitute for scoping keys correctly or rotating
anything that has been exposed.

Two rules kept it from being useless in practice:

1. **Placeholders must not fire.** A checker that cries wolf on
   ``.env.example`` gets disabled within a week. Every pattern below is
   tested against the placeholder forms this repo actually uses.
2. **The Supabase service_role key matters most.** It is a JWT that bypasses
   row-level security entirely. It looks like an innocuous base64 blob, so
   a human reviewer will not spot it in a diff — which is exactly why the
   machine should.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    hint: str


#: Values that look like secrets but are deliberate placeholders.
#: Matched case-insensitively as substrings of the offending line.
ALLOWLIST_MARKERS: tuple[str, ...] = (
    "[your-password]",
    "your-password",
    "[password]",
    "user:password",
    "xxxxxxxx",
    "changeme",
    "change_this_to_a_random_secret_string",
    "test_secret_123",
    "test_groq_key",
    "test_hf_token",
    "example.com",
    "[region]",
    "[project-ref]",
    "abcdefghijklmnopqrst",
    "<your",
    "placeholder",
    "dummy",
    "fake",
    "noqa: secret",
)

#: Files that are allowed to contain placeholder-shaped values.
#:
#: ``test_check_secrets.py`` is here because it necessarily contains
#: realistic-looking fake credentials — they are the fixtures that prove the
#: scanner works. Without this entry the scanner fails on its own test suite,
#: which is both absurd and the fastest route to someone disabling it.
#: The values there are synthetic and documented as such.
ALLOWLIST_PATHS: tuple[str, ...] = (
    ".env.example",
    "scripts/check_secrets.py",
    "scripts/check_db.py",
    "docs/secrets.md",
    "tests/unit/test_check_secrets.py",
)

RULES: tuple[Rule, ...] = (
    Rule(
        "postgres-url-with-password",
        re.compile(r"postgres(?:ql)?://[^\s:/@]+:[^\s@]{6,}@[^\s/]+", re.I),
        "A Postgres connection string with an inline password. "
        "Put it in .env (gitignored) and read it via settings.DATABASE_URL.",
    ),
    Rule(
        "supabase-jwt-key",
        # anon and service_role keys are JWTs: three base64url segments.
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        "A JWT — likely a Supabase anon or service_role key. The service_role "
        "key bypasses row-level security; rotate it immediately if committed.",
    ),
    Rule(
        "groq-api-key",
        re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),
        "A Groq API key. Rotate at console.groq.com and move it to .env.",
    ),
    Rule(
        "huggingface-token",
        re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
        "A HuggingFace token. Rotate at huggingface.co/settings/tokens.",
    ),
    Rule(
        "google-api-key",
        re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}"),
        "A Google/Gemini API key. Rotate in Google AI Studio.",
    ),
    Rule(
        "aws-access-key",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        "An AWS access key ID.",
    ),
    Rule(
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        "A private key block.",
    ),
    Rule(
        "generic-assigned-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*"
            r"[\"']([^\"'\s]{12,})[\"']"
        ),
        "A hardcoded credential-looking assignment.",
    ),
)

SKIP_SUFFIXES = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".zip", ".gz", ".tar", ".whl", ".so", ".dylib", ".woff", ".woff2",
}
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache",
    "_backup_pre_phase0", ".next", "dist", "build",
}


def _is_allowlisted(line: str, path: str) -> bool:
    if any(path.replace("\\", "/").endswith(p) for p in ALLOWLIST_PATHS):
        return True
    lowered = line.lower()
    return any(marker in lowered for marker in ALLOWLIST_MARKERS)


def scan_text(text: str, path: str) -> list[tuple[int, Rule, str]]:
    """Return (line_number, rule, line) for every finding in *text*."""
    findings: list[tuple[int, Rule, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _is_allowlisted(line, path):
            continue
        for rule in RULES:
            if rule.pattern.search(line):
                findings.append((lineno, rule, line.strip()))
                break
    return findings


def _staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True, check=False,
    )
    return [f for f in out.stdout.splitlines() if f.strip()]


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=False
    )
    return [f for f in out.stdout.splitlines() if f.strip()]


def _should_skip(path: str) -> bool:
    p = Path(path)
    if p.suffix.lower() in SKIP_SUFFIXES:
        return True
    return any(part in SKIP_DIRS for part in p.parts)


def _read(path: str, staged: bool) -> str | None:
    """Read the staged blob (what will actually be committed) or the worktree file."""
    if staged:
        out = subprocess.run(
            ["git", "show", f":{path}"], capture_output=True, text=True, check=False
        )
        return out.stdout if out.returncode == 0 else None
    fp = REPO_ROOT / path
    try:
        return fp.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None


def _iter_history_additions() -> "list[tuple[str, int, str]]":
    """Yield (path, index, line) for every line ever ADDED in this repo.

    WHY THIS LIVES HERE AND NOT IN THE WORKFLOW

    The history check used to be a `git log -p | grep -E` pipeline inlined in
    .github/workflows/secrets.yml. It duplicated the patterns above in a second
    dialect (POSIX ERE), and — the reason it failed — it had no notion of
    ALLOWLIST_PATHS, so it fired on this scanner's own test fixtures. A checker
    that cries wolf is a checker someone eventually deletes, which is the first
    principle stated at the top of this file.

    Routing history through the same rules and the same allowlist keeps one
    source of truth and makes the check runnable locally, which is the standard
    .github/workflows/ingest.yml already sets for CI logic.

    Only added lines are examined. A line a commit *removed* was, by
    definition, present in some earlier commit and is caught there.
    """
    out = subprocess.run(
        ["git", "log", "-p", "--all", "--no-color", "--format=%H"],
        capture_output=True, text=True, check=True,
    ).stdout

    results: list[tuple[str, int, str]] = []
    path = "<unknown>"
    for index, line in enumerate(out.splitlines(), 1):
        if line.startswith("+++ "):
            target = line[4:].strip()
            # "+++ b/some/path", or /dev/null for a deletion.
            path = target[2:] if target.startswith(("a/", "b/")) else target
            continue
        # "+++" is handled above; a real addition is "+" followed by content.
        if line.startswith("+") and not line.startswith("+++"):
            results.append((path, index, line[1:]))
    return results


def _scan_history() -> int:
    findings = 0
    for path, index, line in _iter_history_additions():
        if path == "/dev/null" or _should_skip(path):
            continue
        if _is_allowlisted(line, path):
            continue
        for rule in RULES:
            if rule.pattern.search(line):
                if findings == 0:
                    print("\n\033[1;31mSECRET FOUND IN GIT HISTORY\033[0m\n")
                findings += 1
                shown = line if len(line) <= 100 else line[:97] + "..."
                print(f"  \033[1m{path}\033[0m  [{rule.name}]  (history line {index})")
                print(f"    {shown.strip()}")
                print(f"    -> {rule.hint}\n")
                break

    if findings:
        print(f"\033[1;31m{findings} finding(s) in history.\033[0m\n")
        print("Removing the commit is NOT sufficient — anything that ever reached")
        print("a remote must be treated as compromised. Rotate the credential,")
        print("then rewrite history with git-filter-repo if you must.\n")
        return 1

    print("check_secrets: history clean")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan for committed secrets.")
    parser.add_argument(
        "--staged", action="store_true",
        help="Scan only staged content (pre-commit hook mode).",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="Scan every line ever added in git history, not just the current tree.",
    )
    args = parser.parse_args()

    if args.history:
        return _scan_history()

    files = _staged_files() if args.staged else _tracked_files()
    files = [f for f in files if not _should_skip(f)]

    total = 0
    for path in files:
        text = _read(path, staged=args.staged)
        if text is None:
            continue
        for lineno, rule, line in scan_text(text, path):
            if total == 0:
                print("\n\033[1;31mBLOCKED: possible secrets detected\033[0m\n")
            total += 1
            shown = line if len(line) <= 100 else line[:97] + "..."
            print(f"  \033[1m{path}:{lineno}\033[0m  [{rule.name}]")
            print(f"    {shown}")
            print(f"    -> {rule.hint}\n")

    if total:
        print(f"\033[1;31m{total} finding(s).\033[0m Nothing was committed.\n")
        print("If this is a false positive, either use a placeholder value or")
        print("append the comment marker  # noqa: secret  to that line.\n")
        print("If a real credential reached a commit, rotating it is the only")
        print("fix — removing it from the diff does not remove it from history.\n")
        return 1

    scope = "staged files" if args.staged else "tracked files"
    print(f"check_secrets: clean ({len(files)} {scope} scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
