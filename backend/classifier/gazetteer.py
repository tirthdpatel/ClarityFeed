"""Gazetteer matching — the mechanical layer under the country classifier.

Loads country aliases (from the database, or from a list for tests) and finds
which of them occur in a piece of text.

Three decisions worth stating, because each one is a place this could go wrong:

**Longest match wins.** "new delhi" and "delhi" are both aliases. Matching
greedily left-to-right on a length-sorted pattern list means "New Delhi"
consumes both words and scores once as the capital, rather than scoring
"delhi" separately and double-counting the same evidence.

**Each alias scores at most once.** An article that says "India" eight times
is not eight times more about India than one that says it twice. Repetition
measures writing style, not relevance. Distinct aliases matched is the signal.

**Ambiguous aliases are gated, not down-weighted.** An alias marked ambiguous
contributes nothing at all unless one of its context terms appears somewhere
in the text. Down-weighting would still let three weak ambiguous matches
outvote one strong unambiguous one, which is exactly the failure mode
ARCHITECTURE_V2 §A10 is about.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Alias:
    """One gazetteer entry."""

    country_iso2: str
    alias: str
    alias_type: str = "name"
    weight: float = 1.0
    is_ambiguous: bool = False
    context: tuple[str, ...] = ()

    @classmethod
    def from_row(cls, iso2: str, row) -> "Alias":
        ctx = ()
        if getattr(row, "requires_context", None):
            try:
                ctx = tuple(json.loads(row.requires_context))
            except (ValueError, TypeError):
                ctx = ()
        return cls(
            country_iso2=iso2,
            alias=row.alias.lower(),
            alias_type=row.alias_type,
            weight=float(row.weight),
            is_ambiguous=bool(row.is_ambiguous),
            context=ctx,
        )


@dataclass
class Match:
    """An alias found in the text."""

    alias: Alias
    gated_out: bool = False        # ambiguous, and no context term present
    context_hit: str | None = None  # which context term cleared the gate


def normalize(text: str) -> str:
    """Lowercase, fold accents, collapse punctuation to spaces.

    Accent folding matters: feeds spell it "São Paulo" and "Sao Paulo"
    interchangeably, and a gazetteer that only knows one of them silently
    loses half the coverage for that city.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    # Keep alphanumerics and a few in-word marks; everything else is a break.
    text = re.sub(r"[^a-z0-9'\.\- ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class Gazetteer:
    """A compiled set of aliases, ready to match against text."""

    def __init__(self, aliases: Sequence[Alias]) -> None:
        self._aliases = list(aliases)
        # Longest first so greedy alternation prefers "new delhi" over "delhi".
        ordered = sorted(self._aliases, key=lambda a: len(a.alias), reverse=True)
        self._by_surface: dict[str, list[Alias]] = {}
        for a in ordered:
            self._by_surface.setdefault(normalize(a.alias), []).append(a)

        if self._by_surface:
            pattern = "|".join(
                re.escape(s) for s in sorted(self._by_surface, key=len, reverse=True)
            )
            # \b on both sides: "india" must not fire inside "indianapolis".
            self._re = re.compile(rf"\b(?:{pattern})\b")
        else:
            self._re = None

    @property
    def size(self) -> int:
        return len(self._aliases)

    def find(self, text: str) -> list[Match]:
        """Return one Match per distinct alias present in *text*."""
        if self._re is None:
            return []
        norm = normalize(text)
        if not norm:
            return []

        surfaces = {m.group(0) for m in self._re.finditer(norm)}

        matches: list[Match] = []
        for surface in surfaces:
            for alias in self._by_surface.get(surface, ()):
                if alias.is_ambiguous:
                    hit = next(
                        (c for c in alias.context if re.search(rf"\b{re.escape(normalize(c))}\b", norm)),
                        None,
                    )
                    matches.append(Match(alias, gated_out=hit is None, context_hit=hit))
                else:
                    matches.append(Match(alias))
        return matches


def load_from_db(session) -> Gazetteer:
    """Build a Gazetteer from `country_aliases` joined to `countries`.

    Called once per ingestion run, not per article — 255 aliases today and a
    few thousand eventually, all of which fit comfortably in memory.
    """
    from backend.database.orm_models import Country, CountryAlias  # local import

    rows = (
        session.query(CountryAlias, Country.iso2)
        .join(Country, Country.id == CountryAlias.country_id)
        .all()
    )
    return Gazetteer([Alias.from_row(iso2, row) for row, iso2 in rows])
