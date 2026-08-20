"""Tests for the rule-based classifiers.

The emphasis is deliberately lopsided toward *false positives*. Per
ARCHITECTURE_V2 §A10, a wrong country on the homepage is the worst failure
this product has, while a missed country only means an article is filed under
International. These tests are written to make the expensive failure loud.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.classifier import (
    Alias, CategoryClassifier, CountryClassifier, Gazetteer,
    load_rules_from_yaml, normalize,
)

DATA = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture(scope="module")
def gazetteer() -> Gazetteer:
    import yaml
    aliases = []
    for path in sorted((DATA / "gazetteer").glob("*.yaml")):
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


@pytest.fixture(scope="module")
def country(gazetteer) -> CountryClassifier:
    return CountryClassifier(gazetteer)


@pytest.fixture(scope="module")
def category() -> CategoryClassifier:
    return CategoryClassifier(load_rules_from_yaml(DATA / "categories.yaml"))


# --------------------------------------------------------------- normalize
class TestNormalize:
    def test_folds_accents(self):
        # Feeds spell it both ways; a gazetteer that knows only one loses half
        # the coverage for that city.
        assert normalize("São Paulo") == "sao paulo"

    def test_lowercases_and_strips_punctuation(self):
        assert normalize("New Delhi — 2026!") == "new delhi 2026"

    def test_empty(self):
        assert normalize("") == ""


# --------------------------------------------------------------- gazetteer
class TestGazetteerMatching:
    def test_word_boundary_prevents_substring_match(self, gazetteer):
        """'india' must not fire inside 'Indianapolis'."""
        hits = {m.alias.country_iso2 for m in gazetteer.find("Indianapolis hosted the event")
                if not m.gated_out}
        assert "IN" not in hits

    def test_longest_match_wins(self, gazetteer):
        """'New Delhi' scores once as the capital, not twice with 'Delhi'."""
        found = [m.alias.alias for m in gazetteer.find("New Delhi announced") if not m.gated_out]
        assert "new delhi" in found
        assert "delhi" not in found

    def test_repetition_does_not_multiply(self, gazetteer):
        once = gazetteer.find("India")
        many = gazetteer.find("India India India India India")
        assert len(once) == len(many)


class TestAmbiguityGating:
    def test_gate_closed_without_context(self, gazetteer):
        ms = [m for m in gazetteer.find("Georgia protests continue in Tbilisi")
              if m.alias.alias == "georgia"]
        assert ms and all(m.gated_out for m in ms)

    def test_gate_opens_with_context(self, gazetteer):
        ms = [m for m in gazetteer.find("Georgia Senate race tightens in Atlanta")
              if m.alias.alias == "georgia" and m.alias.country_iso2 == "US"]
        assert ms and any(not m.gated_out for m in ms)

    def test_every_ambiguous_alias_has_a_gate(self, gazetteer):
        """An ambiguous alias with no context terms would be ungated in
        practice — the exact bug this design exists to prevent."""
        bad = [a.alias for a in gazetteer._aliases if a.is_ambiguous and not a.context]
        assert bad == [], f"ambiguous aliases without context: {bad}"


# ------------------------------------------------------- country: the traps
@pytest.mark.parametrize("title,description,expected", [
    ("Georgia Senate race tightens as Atlanta turnout surges", "", "US"),
    ("London mayor announces Thames crossing plan", "", "GB"),
    ("Perth records hottest day as Western Australia swelters", "", "AU"),
    ("Sydney Opera House marks anniversary in New South Wales", "", "AU"),
    ("Victoria state government announces Melbourne rail upgrade", "", "AU"),
    ("Congress party leader Rahul Gandhi addresses Lok Sabha", "", "IN"),
    ("Congress passes spending bill as Senate returns to Washington", "", "US"),
])
def test_known_traps_resolve_correctly(country, title, description, expected):
    assert country.classify(title, description).primary == expected


@pytest.mark.parametrize("title,forbidden", [
    ("Georgia protests continue for a third night in Tbilisi", "US"),
    ("Nice weather expected across the region this weekend", "FR"),
    ("Reading the fine print on the new contract", "GB"),
    ("Amazon reports record quarterly earnings", "BR"),
    ("Turkey prices rise ahead of the holiday season", "TR"),
    ("The diet industry faces new advertising rules", "JP"),
])
def test_common_words_do_not_produce_false_countries(country, title, forbidden):
    """The expensive failure mode: asserting a country that is not there."""
    assert country.classify(title).primary != forbidden


class TestCountryDecision:
    def test_abstains_when_there_is_no_evidence(self, country):
        r = country.classify("Scientists discover new species of deep-sea coral")
        assert r.primary is None
        assert r.is_international

    def test_source_prior_alone_assigns_but_is_flagged(self, country):
        """Right often enough to use, never confident enough to trust."""
        r = country.classify("Council approves new budget", source_country="IN")
        assert r.primary == "IN"
        assert r.needs_review is True
        assert r.confidence <= 0.35
        assert r.method == "source"

    def test_content_evidence_beats_source_prior(self, country):
        r = country.classify("Bundestag approves budget in Berlin", source_country="IN")
        assert r.primary == "DE"

    def test_content_backed_result_is_confident(self, country):
        r = country.classify("Modi announces policy in New Delhi", source_country="IN")
        assert r.primary == "IN"
        assert r.needs_review is False
        assert r.confidence > 0.7

    def test_bilateral_story_lists_both(self, country):
        r = country.classify(
            "India and Britain sign trade agreement",
            "Officials from New Delhi and London finalised terms",
        )
        assert {r.primary, *r.secondary} >= {"IN", "GB"}


# ------------------------------------------------------------- category
class TestCategory:
    @pytest.mark.parametrize("title,expected", [
        ("OpenAI releases new large language model", "ai"),
        ("Formula 1 driver takes pole position at the grand prix", "motorsport"),
        ("Ransomware attack hits hospital network", "cybersecurity"),
        ("COP30 delegates agree on emissions targets", "climate"),
    ])
    def test_assigns_expected_category(self, category, title, expected):
        assert category.classify(title).primary == expected

    def test_ai_is_a_child_of_technology(self):
        rules = {r.slug: r for r in load_rules_from_yaml(DATA / "categories.yaml")}
        assert rules["ai"].parent_slug == "technology"

    @pytest.mark.parametrize("title", [
        "Airline announces budget fares for summer",       # 'budget' != politics
        "The ruling party lost its majority",              # 'ruling' != law
        "He shares his story of recovery",                 # 'shares' != markets
    ])
    def test_generic_words_do_not_trigger_categories(self, category, title):
        """These all fired before the keywords were disambiguated."""
        assert category.classify(title).primary is None

    def test_market_rally_is_not_motorsport(self, category):
        r = category.classify("Sensex closes higher as markets rally")
        assert r.primary == "markets"
        assert "motorsport" not in r.secondary

    def test_abstains_when_nothing_matches(self, category):
        r = category.classify("Something completely unclassifiable happened")
        assert r.primary is None
        assert r.needs_review

    def test_no_keyword_is_shared_between_categories(self):
        """A shared keyword makes the winner arbitrary."""
        rules = load_rules_from_yaml(DATA / "categories.yaml")
        seen, dupes = {}, []
        for r in rules:
            for k in r.keywords:
                if k in seen:
                    dupes.append((k, seen[k], r.slug))
                seen[k] = r.slug
        assert dupes == [], f"shared keywords: {dupes}"
