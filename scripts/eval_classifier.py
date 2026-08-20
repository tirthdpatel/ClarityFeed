#!/usr/bin/env python3
"""Score the country classifier against data/eval/country_cases.yaml.

    python scripts/eval_classifier.py            # summary
    python scripts/eval_classifier.py --verbose  # every case
    python scripts/eval_classifier.py --tag georgia

Reads the gazetteer from the YAML files rather than the database, so it runs
without a connection and scores exactly what is in version control.

WHAT THIS IS NOT: an accuracy figure for the live corpus. The cases are
hand-written around known traps, so the numbers here are a regression signal.
The real number needs the 200 hand-labelled articles from V3 Part F9.
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.classifier.country import CountryClassifier  # noqa: E402
from backend.classifier.gazetteer import Alias, Gazetteer  # noqa: E402

G, R, Y, DIM, B, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


def load_gazetteer_from_yaml() -> Gazetteer:
    aliases = []
    for path in sorted((REPO_ROOT / "data" / "gazetteer").glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        d = yaml.safe_load(path.read_text())
        for a in d["aliases"]:
            aliases.append(Alias(
                country_iso2=d["country"], alias=a["alias"].lower(),
                alias_type=a.get("type", "name"), weight=float(a["weight"]),
                is_ambiguous=bool(a.get("ambiguous", False)),
                context=tuple(a.get("context", ())),
            ))
    return Gazetteer(aliases)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--tag")
    args = ap.parse_args()

    gaz = load_gazetteer_from_yaml()
    clf = CountryClassifier(gaz)
    cases = yaml.safe_load((REPO_ROOT / "data" / "eval" / "country_cases.yaml").read_text())["cases"]
    if args.tag:
        cases = [c for c in cases if args.tag in c["tags"]]

    print(f"\n{B}Country classifier evaluation{RESET}")
    print(f"{DIM}gazetteer: {gaz.size} aliases   cases: {len(cases)}{RESET}\n")

    by_tag = collections.defaultdict(lambda: [0, 0])
    passed = failed = 0
    failures = []
    # A false positive is a wrong country asserted. A miss is an abstention
    # where a country was expected. They are counted separately because §A10
    # says they are not equally costly.
    false_positives = misses = 0

    for c in cases:
        r = clf.classify(
            c["title"], c.get("description", ""),
            c.get("source_country"), c.get("feed_url", ""),
        )
        expect = c.get("expect")
        allow = c.get("allow")
        must_not = c.get("must_not", [])

        if allow:
            ok = r.primary in allow
        elif expect is None:
            ok = r.primary is None
        else:
            ok = r.primary == expect
        if must_not and r.primary in must_not:
            ok = False

        if not ok:
            if expect is None and r.primary is not None:
                false_positives += 1
            elif expect is not None and r.primary is None:
                misses += 1
            else:
                false_positives += 1

        for t in c["tags"]:
            by_tag[t][1] += 1
            if ok:
                by_tag[t][0] += 1

        if ok:
            passed += 1
            if args.verbose:
                print(f"  {G}PASS{RESET} {c['title'][:60]:62} -> {r.primary}")
        else:
            failed += 1
            failures.append((c, r, expect, allow, must_not))
            if args.verbose:
                print(f"  {R}FAIL{RESET} {c['title'][:60]:62} -> {r.primary} (want {allow or expect})")

    if failures and not args.verbose:
        print(f"{B}Failures{RESET}")
        for c, r, expect, allow, must_not in failures:
            want = allow or expect or "abstain"
            print(f"  {R}x{RESET} {c['title'][:66]}")
            print(f"      got {r.primary} conf={r.confidence}  want {want}  [{','.join(c['tags'])}]")
            print(f"      {DIM}{r.reason}  scores={r.scores}{RESET}")
        print()

    print(f"{B}By trap{RESET}")
    for tag in sorted(by_tag, key=lambda t: (by_tag[t][0] / by_tag[t][1], t)):
        ok, tot = by_tag[tag]
        colour = G if ok == tot else (Y if ok else R)
        print(f"  {colour}{ok}/{tot}{RESET}  {tag}")

    pct = 100 * passed / max(1, len(cases))
    print(f"\n{B}{passed}/{len(cases)} passed ({pct:.0f}%){RESET}")
    print(f"{DIM}  false positives (wrong country asserted): {false_positives}")
    print(f"  misses (abstained when a country was expected): {misses}")
    print(f"  A false positive is the expensive one — see V2 §A10.{RESET}\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
