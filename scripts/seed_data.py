#!/usr/bin/env python3
"""Load data/*.yaml into the database.

    python scripts/seed_data.py            # load / update
    python scripts/seed_data.py --dry-run  # report what would change

Idempotent by natural key: regions by `code`, countries by `iso2`, categories
by `slug`, languages by `code`. Re-running after editing a YAML file updates
changed rows and leaves the rest alone, so this is the normal way to evolve
seed data rather than a one-shot import.

Gazetteer aliases are replaced wholesale per country. Upserting would leave
deleted aliases behind, and a stale alias silently mis-files articles forever
— exactly the failure ARCHITECTURE_V2 §A10 warns about.

DESIGN NOTE — why everything is bulk
------------------------------------
The first version issued one SELECT per row to check existence, then one
INSERT per row. Roughly 200 round trips. Against a local Postgres that is
imperceptible; against Frankfurt from India at ~250ms RTT it took over a
minute and timed out.

Latency, not throughput, is the constraint when seeding a remote database.
So each table is now: one SELECT for everything that exists, diff in Python,
one bulk INSERT and one bulk UPDATE. About a dozen round trips total.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml
from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.database.orm_models import (  # noqa: E402
    CategoryDef, Country, CountryAlias, Language, Region,
)
from config.settings import settings  # noqa: E402

DATA = REPO_ROOT / "data"
GREEN, YELLOW, DIM, BOLD, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


def _load(name: str):
    return yaml.safe_load((DATA / name).read_text())


def _sync(s: Session, model, key_field: str, desired: list[dict], dry: bool) -> tuple[int, int]:
    """Reconcile a table against `desired` using one SELECT + bulk write.

    `desired` rows must each contain `key_field`. Returns (created, updated).
    """
    existing = {getattr(r, key_field): r for r in s.scalars(select(model))}
    to_insert, to_update = [], []

    for row in desired:
        key = row[key_field]
        current = existing.get(key)
        if current is None:
            to_insert.append(row)
        else:
            diff = {k: v for k, v in row.items()
                    if k != key_field and getattr(current, k) != v}
            if diff:
                to_update.append({"_pk": current.id, **diff})

    if not dry:
        if to_insert:
            s.execute(insert(model), to_insert)
        for u in to_update:
            pk = u.pop("_pk")
            s.execute(update(model).where(model.id == pk).values(**u))

    # Rows in the database that the YAML no longer mentions. NOT deleted —
    # they may carry article associations, and silently dropping a category
    # would orphan real classifications. Reported so a rename shows up as
    # "one created, one orphaned" instead of quietly doubling the table,
    # which is exactly how a stray 'policy framework' slug once slipped in.
    desired_keys = {r[key_field] for r in desired}
    orphans = sorted(set(existing) - desired_keys)
    return len(to_insert), len(to_update), orphans


def seed_regions(s, dry):
    return _sync(s, Region, "code", [
        {"code": r["code"], "name": r["name"], "sort_order": r["sort_order"]}
        for r in _load("regions.yaml")
    ], dry)


def seed_countries(s, dry):
    if not dry:
        s.flush()
    regions = {r.code: r.id for r in s.scalars(select(Region))}
    return _sync(s, Country, "iso2", [
        {
            "iso2": c["iso2"], "iso3": c["iso3"], "name": c["name"], "slug": c["slug"],
            "region_id": regions.get(c["region"]),
            "default_language": c["default_language"], "flag_emoji": c["flag"],
            "is_enabled": c["is_enabled"], "sort_order": c["sort_order"],
        }
        for c in _load("countries.yaml")
    ], dry)


def seed_categories(s, dry):
    """Two passes: rows first, then parents.

    A single pass would break whenever a child precedes its parent in the
    file — an ordering property the data should not have to guarantee.
    """
    defs = _load("categories.yaml")
    created, updated, orphans = _sync(s, CategoryDef, "slug", [
        {
            "slug": c["slug"], "name": c["name"],
            "keywords": json.dumps(c["keywords"]) if c.get("keywords") else None,
            "color_token": c.get("color_token"), "sort_order": c["sort_order"],
        }
        for c in defs
    ], dry)

    if not dry:
        s.flush()
        by_slug = {c.slug: c for c in s.scalars(select(CategoryDef))}
        for c in defs:
            if c.get("parent"):
                child, parent = by_slug[c["slug"]], by_slug[c["parent"]]
                if child.parent_id != parent.id:
                    child.parent_id = parent.id
    return created, updated, orphans


def seed_languages(s, dry):
    return _sync(s, Language, "code", [
        {
            "code": l["code"], "name": l["name"], "native_name": l["native_name"],
            "direction": l["direction"], "translation_tier": l["translation_tier"],
            "is_enabled": l["is_enabled"], "sort_order": l["sort_order"],
        }
        for l in _load("languages.yaml")
    ], dry)


def seed_gazetteer(s, dry) -> tuple[int, int]:
    """Replace all alias sets in one DELETE and one INSERT."""
    if not dry:
        s.flush()
    countries = {c.iso2: c.id for c in s.scalars(select(Country))}
    rows, skipped, seen_ids = [], 0, []

    for path in sorted((DATA / "gazetteer").glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        d = yaml.safe_load(path.read_text())
        cid = countries.get(d["country"])
        if cid is None:
            skipped += 1
            continue
        seen_ids.append(cid)
        for a in d["aliases"]:
            rows.append({
                "country_id": cid,
                "alias": a["alias"].lower(),
                "alias_type": a.get("type", "name"),
                "weight": float(a["weight"]),
                "is_ambiguous": bool(a.get("ambiguous", False)),
                "requires_context": json.dumps(a["context"]) if a.get("context") else None,
            })

    if not dry and seen_ids:
        s.execute(delete(CountryAlias).where(CountryAlias.country_id.in_(seen_ids)))
        if rows:
            s.execute(insert(CountryAlias), rows)
    return len(rows), skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    engine = create_engine(settings.DATABASE_URL)
    print(f"\n{BOLD}Seeding ClarityFeed reference data{RESET}")
    if args.dry_run:
        print(f"{YELLOW}DRY RUN — nothing will be written{RESET}")
    print()

    t0 = time.monotonic()
    with Session(engine) as s:
        all_orphans: dict[str, list[str]] = {}
        for label, fn in (
            ("regions", seed_regions), ("countries", seed_countries),
            ("categories", seed_categories), ("languages", seed_languages),
        ):
            a, b, orphans = fn(s, args.dry_run)
            note = f", {YELLOW}{len(orphans)} orphaned{RESET}" if orphans else ""
            print(f"  {label:12} {GREEN}+{a}{RESET} created, {b} updated{note}")
            if orphans:
                all_orphans[label] = orphans

        if all_orphans:
            print(f"\n{YELLOW}  Rows in the database that the YAML no longer defines:{RESET}")
            for label, items in all_orphans.items():
                print(f"    {label}: {', '.join(items)}")
            print(f"{DIM}    Not deleted — they may have article associations."
                  f" Remove by hand once you have checked.{RESET}")

        loaded, skipped = seed_gazetteer(s, args.dry_run)
        print(f"  {'gazetteer':12} {GREEN}{loaded}{RESET} aliases"
              + (f", {YELLOW}{skipped} skipped (unknown country){RESET}" if skipped else ""))

        if args.dry_run:
            s.rollback()
        else:
            s.commit()

        counts = {
            "regions": s.scalar(select(Region).with_only_columns(Region.id).order_by(None).count())
            if False else len(list(s.scalars(select(Region.id)))),
            "countries": len(list(s.scalars(select(Country.id)))),
            "categories": len(list(s.scalars(select(CategoryDef.id)))),
            "languages": len(list(s.scalars(select(Language.id)))),
            "aliases": len(list(s.scalars(select(CountryAlias.id)))),
        }

    dt = time.monotonic() - t0
    if not args.dry_run:
        print(f"\n{GREEN}{BOLD}Committed{RESET} in {dt:.1f}s")
    print(f"{DIM}  " + "  ".join(f"{k}={v}" for k, v in counts.items()) + f"{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
