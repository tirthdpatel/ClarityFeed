"""Rule-based category classification.

Keyword matching over the taxonomy in `category_defs`. Costs zero API calls,
which is the entire point — ARCHITECTURE_V3 §A1 showed the LLM approach
budgeted ~9,600 calls/day against a 1,000/day ceiling.

Assigns the MOST SPECIFIC matching category. The parent is implied by the
tree and never stored twice: an article tagged `ai` appears in Technology
feeds through the parent relationship, not a second row.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from backend.classifier.gazetteer import normalize

MIN_SCORE = 1.0          # at least one solid keyword hit
MULTI_LABEL_RATIO = 0.6  # a second category needs 60% of the winner's score


@dataclass(frozen=True)
class CategoryRule:
    slug: str
    name: str
    parent_slug: str | None
    keywords: tuple[str, ...]

    @classmethod
    def from_row(cls, row, parent_slug: str | None) -> "CategoryRule":
        kws = ()
        if getattr(row, "keywords", None):
            try:
                kws = tuple(k.lower() for k in json.loads(row.keywords))
            except (ValueError, TypeError):
                kws = ()
        return cls(row.slug, row.name, parent_slug, kws)


@dataclass
class CategoryResult:
    primary: str | None
    confidence: float
    secondary: list[str] = field(default_factory=list)
    needs_review: bool = False
    scores: dict[str, float] = field(default_factory=dict)
    matched: dict[str, list[str]] = field(default_factory=dict)


class CategoryClassifier:
    def __init__(self, rules: list[CategoryRule]) -> None:
        # Only leaf categories carry keywords; top-level ones are reached
        # through the parent link.
        self._rules = [r for r in rules if r.keywords]
        self._patterns = {
            r.slug: [
                (kw, re.compile(rf"\b{re.escape(normalize(kw))}\b"))
                for kw in r.keywords
            ]
            for r in self._rules
        }
        self._parent = {r.slug: r.parent_slug for r in rules}

    def classify(self, title: str, description: str = "") -> CategoryResult:
        # Title carries more signal than body text — a keyword in the headline
        # is an editorial choice, one in paragraph six is often incidental.
        t_norm, d_norm = normalize(title), normalize(description)

        scores: dict[str, float] = {}
        matched: dict[str, list[str]] = {}

        for rule in self._rules:
            score = 0.0
            hits: list[str] = []
            for kw, pat in self._patterns[rule.slug]:
                in_title = bool(pat.search(t_norm))
                in_desc = bool(pat.search(d_norm))
                if in_title:
                    score += 1.0
                    hits.append(kw)
                elif in_desc:
                    score += 0.5
                    hits.append(kw)
            if score:
                scores[rule.slug] = score
                matched[rule.slug] = hits

        if not scores:
            return CategoryResult(primary=None, confidence=0.0, needs_review=True)

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_slug, top_score = ranked[0]

        if top_score < MIN_SCORE:
            return CategoryResult(
                primary=None, confidence=round(top_score, 2), needs_review=True,
                scores={k: round(v, 2) for k, v in ranked}, matched=matched,
            )

        secondary = [
            s for s, sc in ranked[1:]
            if sc >= top_score * MULTI_LABEL_RATIO and self._parent.get(s) != self._parent.get(top_slug)
        ]
        conf = min(1.0, top_score / 3.0)

        return CategoryResult(
            primary=top_slug,
            confidence=round(conf, 2),
            secondary=secondary[:2],
            needs_review=conf < 0.4,
            scores={k: round(v, 2) for k, v in ranked},
            matched=matched,
        )


def load_rules_from_yaml(path) -> list[CategoryRule]:
    import yaml
    defs = yaml.safe_load(path.read_text())
    return [
        CategoryRule(
            slug=c["slug"], name=c["name"], parent_slug=c.get("parent"),
            keywords=tuple(k.lower() for k in c.get("keywords", ())),
        )
        for c in defs
    ]
