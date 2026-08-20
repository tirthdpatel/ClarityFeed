"""Rule-based country classification — Tiers 0–3 of ARCHITECTURE_V2 §8.

    Tier 0  source prior      the publisher's own country
    Tier 1  feed section      /news/india/ in the feed URL
    Tier 2  gazetteer         weighted aliases over title + description
    Tier 3  decision          score, margin, confidence, review flag

Tier 4 (LLM adjudication for low-confidence cases) is Phase 5 and is
deliberately absent — the point of this design is that classification costs
zero API calls and cannot be broken by a vendor deprecation.

The governing asymmetry, from §A10: **missing a country costs one article
filed under International; a wrong country puts a US crime story on the
Georgia country page.** Every threshold here is tuned toward abstaining
rather than guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.classifier.gazetteer import Gazetteer, Match, normalize

# ---------------------------------------------------------------------------
# Tunables. These live here rather than in settings because they are only
# meaningful together — changing one without the others produces nonsense.
# They are what the gold set in data/eval/ exists to calibrate.
# ---------------------------------------------------------------------------

SOURCE_PRIOR_WEIGHT = 0.65   # a publisher country is decent evidence; must be able to clear MIN_SCORE alone
SECTION_PRIOR_WEIGHT = 0.70  # a feed section is stronger — it is an editorial claim

MIN_SCORE = 0.60             # below this, no country is assigned at all
MIN_MARGIN = 0.25            # top must beat second by this or it is low-confidence
SECONDARY_MIN = 0.50         # a country needs this to be listed as secondary
INTERNATIONAL_MIN_COUNTRIES = 3   # this many real contenders -> international


@dataclass
class CountryScore:
    iso2: str
    score: float
    matched: list[str] = field(default_factory=list)
    from_source: bool = False
    from_section: bool = False


@dataclass
class CountryResult:
    """The outcome for one article."""

    primary: str | None
    confidence: float
    secondary: list[str] = field(default_factory=list)
    mentioned: list[str] = field(default_factory=list)
    is_international: bool = False
    needs_review: bool = False
    method: str = "gazetteer"
    scores: dict[str, float] = field(default_factory=dict)
    reason: str = ""

    @property
    def all_countries(self) -> list[str]:
        out = [self.primary] if self.primary else []
        return out + self.secondary + self.mentioned


def extract_section_country(feed_url: str, gazetteer_countries: set[str]) -> str | None:
    """Tier 1 — read a country out of the feed URL's path.

    `https://example.com/news/india/rss.xml` is an editorial statement by the
    publisher that this feed is about India, and it is worth more than any
    single keyword in the body. Matches only on full path segments, so
    `/indianapolis/` cannot masquerade as `/india/`.
    """
    if not feed_url:
        return None
    slug_to_iso = {c.lower(): c for c in gazetteer_countries}
    # Long names first so "south-africa" is not shadowed by "africa".
    names = {
        "india": "IN", "united-states": "US", "usa": "US", "us": "US",
        "uk": "GB", "britain": "GB", "united-kingdom": "GB",
        "australia": "AU", "canada": "CA", "germany": "DE", "france": "FR",
        "japan": "JP", "brazil": "BR", "south-africa": "ZA", "singapore": "SG",
        "uae": "AE", "emirates": "AE",
    }
    segments = [s for s in re.split(r"[/\.\?=&]+", feed_url.lower()) if s]
    for seg in segments:
        if seg in names:
            return names[seg]
        if seg.upper() in slug_to_iso:
            return slug_to_iso[seg.upper()]
    return None


class CountryClassifier:
    """Assigns countries to articles using the gazetteer and priors."""

    def __init__(self, gazetteer: Gazetteer) -> None:
        self._gaz = gazetteer
        self._known = {a.country_iso2 for a in gazetteer._aliases}

    def classify(
        self,
        title: str,
        description: str = "",
        source_country: str | None = None,
        feed_url: str = "",
    ) -> CountryResult:
        text = f"{title}. {description}".strip()

        # -- Tier 2: gazetteer -------------------------------------------
        scores: dict[str, CountryScore] = {}

        def bump(iso2: str, amount: float, label: str, **flags):
            cs = scores.setdefault(iso2, CountryScore(iso2=iso2, score=0.0))
            cs.score += amount
            if label:
                cs.matched.append(label)
            for k, v in flags.items():
                setattr(cs, k, v)

        for m in self._gaz.find(text):
            if m.gated_out:
                continue
            bump(m.alias.country_iso2, m.alias.weight, m.alias.alias)

        # -- Tier 0: source prior ----------------------------------------
        if source_country:
            sc = source_country.strip().upper()
            if sc in self._known:
                bump(sc, SOURCE_PRIOR_WEIGHT, "", from_source=True)

        # -- Tier 1: feed section ----------------------------------------
        section = extract_section_country(feed_url, self._known)
        if section:
            bump(section, SECTION_PRIOR_WEIGHT, "", from_section=True)

        if not scores:
            return CountryResult(
                primary=None, confidence=0.0, is_international=True,
                method="none", reason="no country evidence found",
            )

        ranked = sorted(scores.values(), key=lambda s: s.score, reverse=True)
        top = ranked[0]
        second_score = ranked[1].score if len(ranked) > 1 else 0.0
        margin = top.score - second_score

        score_map = {s.iso2: round(s.score, 3) for s in ranked}

        # -- Tier 3: decision --------------------------------------------
        if top.score < MIN_SCORE:
            return CountryResult(
                primary=None, confidence=round(top.score, 3),
                is_international=True, needs_review=True,
                method="gazetteer", scores=score_map,
                reason=f"top score {top.score:.2f} below MIN_SCORE {MIN_SCORE}",
            )

        contenders = [s for s in ranked if s.score >= SECONDARY_MIN]
        is_intl = len(contenders) >= INTERNATIONAL_MIN_COUNTRIES

        # Confidence blends absolute evidence with how clearly it won. A high
        # score that barely beats a rival is not a confident answer.
        conf = min(1.0, (top.score / 2.0) * 0.6 + min(margin, 1.0) * 0.4)

        # A country resting only on the publisher's own nationality is an
        # inference about the newsroom, not evidence from the article. It is
        # right often enough to be worth assigning (§8 Tier 0) but it should
        # never masquerade as a confident answer, so it is capped and flagged.
        source_only = top.from_source and not top.matched and not top.from_section
        if source_only:
            conf = min(conf, 0.35)

        needs_review = margin < MIN_MARGIN or source_only
        reason = (
            f"top={top.iso2}:{top.score:.2f} margin={margin:.2f}"
            + (" (source prior only — flagged)" if source_only else "")
            + (" (below MIN_MARGIN — flagged for review)" if margin < MIN_MARGIN else "")
        )

        secondary = [s.iso2 for s in ranked[1:] if s.score >= SECONDARY_MIN]
        mentioned = [s.iso2 for s in ranked[1:] if 0 < s.score < SECONDARY_MIN]

        method = "source" if (top.from_source and not top.matched) else "gazetteer"
        if top.from_section and not top.matched:
            method = "section"

        return CountryResult(
            primary=top.iso2,
            confidence=round(conf, 3),
            secondary=secondary,
            mentioned=mentioned,
            is_international=is_intl,
            needs_review=needs_review,
            method=method,
            scores=score_map,
            reason=reason,
        )
