# ClarityFeed v3 — Amendments to Architecture V2

**Status:** Approved for implementation. Phase 0 in progress.
**Date:** 19 August 2026
**Supersedes:** `ARCHITECTURE_V2.md` §2 (LLM row), §3.3, §8 (category section), §12, §13, §15, §16.
**Leaves intact:** everything else in V2. Read that document first; this one records what changed and specifies what it did not cover.

---

## 0. Why this document exists

Three things happened after V2 was written on 9 August:

1. **A fact in V2 expired.** V2 said llama-3.1-8b-instant was "announced for deprecation in June 2026." Groq deprecated it on **16 August 2026**. Both models named in `config/settings.py` are now dead, not dying. This moved from a planned fix to a live outage.
2. **New product requirements.** Reader-selectable languages with translated content, an explicit category taxonomy including AI, and multi-country selection.
3. **Gaps surfaced on review.** V2 has no concept of breaking news, no accuracy measurement for its own classifier, and omits roughly eight things every news site has.

V2's architecture survives all of this unchanged. Ingestion still belongs in GitHub Actions, reads still never touch Postgres on the hot path, the permission model is still the legal core. What follows are amendments, not a redesign.

---

# PART A — Corrections to V2

## A1. The Groq situation, as of today

Verified against `console.groq.com/docs/deprecations` and `/docs/rate-limits` on 19 August 2026.

| Model | Status |
|---|---|
| `mixtral-8x7b-32768` | Deprecated March 2025. Gone. |
| `llama-3.1-8b-instant` | **Deprecated 16 August 2026.** Replacement: `openai/gpt-oss-20b` |
| `llama-3.3-70b-versatile` | Deprecated August 2026. Replacement: `openai/gpt-oss-120b` or `qwen/qwen3.6-27b` |

Current free-tier limits for the replacement models: **30 RPM / 1,000 RPD / 8K TPM / 200K TPD.**

V2's finding A1 was correct and is now urgent: the current config does not merely exceed quota, it names models that return 404. Phase 0 replaces them and puts a provider abstraction in front so the next deprecation is a config change rather than a code change.

**The 1,000 RPD ceiling is the hard planning constraint for every LLM feature in this document.** Every proposal below states its call cost against that number.

## A2. Revised daily LLM budget

| Consumer | Calls/day | Notes |
|---|---|---|
| Classification (country + category) | **0** | Rules-based, per V2 §8. Unchanged. |
| Summaries — top clusters only | ~40 | Batched 10/call → **4 calls** |
| Tier-4 classification adjudication | ~60 low-confidence articles, batched 20/call → **3 calls** | Phase 5 |
| Quality-tier translation | **0 Groq** | Uses Azure free tier, not the LLM. See Part B. |
| **Total** | **~10 calls/day** | Against a 1,000 ceiling |

Two orders of magnitude of headroom. That is deliberate — it means a provider outage, a quota cut, or a doubling of volume are all survivable without an architecture change.

---

# PART B — Language and translation

**The requirement:** a reader chooses the language they read the news in, and sees content in that language.

This is the largest addition in v3 and the one with the most ways to get it wrong.

## B1. Why the translation APIs do not fit

Your storable text per article is bounded by the permission model in V2 §3.6: a title (~70 chars), a description capped at `max_description_chars = 300`, and sometimes a generated summary. Call it **400 characters per article.**

At the ~1,200 articles/day V2 projects, that is **14.4M characters per month per target language.**

| Service | Free/month | Covers | Card? |
|---|---|---|---|
| Azure Translator F0 | 2,000,000 | **14%** of one language | No |
| DeepL API Free | 500,000 | 3.5% of one language | No |
| Google Cloud Translation | 500,000 | 3.5% of one language | Yes |

The most generous free tier in the market covers one seventh of a single language. Translating everything through an API is not a budgeting problem, it is an impossibility. Anything built on that assumption fails in week one.

## B2. The design: translate in the runner, in two tiers

**Tier 1 — Argos Translate, local, unlimited.**

[Argos Translate](https://github.com/argosopentech/argos-translate) is an offline neural translation library: `pip install argostranslate`, dual MIT/CC0, 35+ languages, CPU-only, and it auto-pivots through English for pairs without a direct model. It runs inside the GitHub Actions runner — which under V2 §1 already has 7 GB of RAM and is already loading `sentence-transformers` for MiniLM embeddings.

Zero API calls. Zero quota. Zero cost. Unlimited volume. The cost is runner minutes, which are free on public repos, and a one-time ~100–200 MB model download per language pair, cached with `actions/cache`.

The honest tradeoff: Argos output is measurably below DeepL. For headlines and 300-character descriptions it is generally serviceable. For nuance it is rough, and on a news site a rough translation is occasionally a wrong one.

**Tier 2 — Azure Translator F0, for what gets read.**

Azure's free tier gives 2M characters/month with no credit card and, critically, **returns 429 rather than billing you** when exhausted. Applied only to the top ~50 clusters per day:

```
50 clusters × 400 chars × 30 days = 600,000 chars/month per language
2,000,000 / 600,000 ≈ 3 languages at quality tier, free
```

This is the same principle V2 §A1 already established for summaries — *do the expensive thing only for what people actually read* — extended to translation.

**Tier 0 — original.** If an article is already in the reader's language, it is served as-is with `method = 'original'`. No translation, no cost, best quality.

## B3. What is explicitly not being built

**Full-article translation.** Arithmetically impossible on free tiers, and mostly moot: the permission model means full text does not exist for most sources anyway.

**Request-time translation.** Translate once at ingest, store, serve from ISR cache forever. Translating on request would blow quota instantly, add latency to every page, and — the decisive objection — leave no translated text in the server-rendered HTML, so search engines could not index the translated pages at all. A translated page Google cannot see is worth close to nothing.

**Chrome's on-device Translator API.** Genuinely free and zero-infrastructure, but client-side only, so it has the same fatal SEO property. Viable later as a progressive enhancement for languages we do not support server-side. Not a foundation.

## B4. Schema

```
languages
    id, code VARCHAR(10) UNIQUE          -- BCP-47: en, hi, ar, es, fr, pt
    name, native_name                     -- "Hindi" / "हिन्दी"
    direction ENUM(ltr|rtl) NOT NULL DEFAULT 'ltr'
    is_enabled BOOL, sort_order INT
    translation_tier ENUM(none|argos|azure) NOT NULL DEFAULT 'argos'

article_translations
    article_id  → raw_articles(id) ON DELETE CASCADE
    language_id → languages(id)
    title TEXT NOT NULL
    description TEXT
    method ENUM(original|argos|azure|llm|manual) NOT NULL
    created_at TIMESTAMPTZ
    PRIMARY KEY (article_id, language_id)

cluster_translations
    cluster_id  → story_clusters(id) ON DELETE CASCADE
    language_id → languages(id)
    canonical_title TEXT NOT NULL
    summary_tldr TEXT
    method ENUM(...), created_at
    PRIMARY KEY (cluster_id, language_id)
```

`native_name` is not decoration — a language switcher that lists "Hindi" to someone who reads only Hindi is a broken language switcher. It lists "हिन्दी".

`translation_tier` per language is what makes the Azure budget manageable: promote a language to `azure` when it earns the traffic, demote it when it does not, with no code change.

Index: `article_translations (language_id, article_id)` for the language-filtered feed join.

## B5. Where it sits in the pipeline

Translation goes **after the publish barrier**, alongside enrichment:

```
Classification → Embedding → Clustering → PUBLISH ← site is live here
                                             ↓
                              Enrichment (best-effort, never blocks)
                                  ├── LLM summaries (top clusters)
                                  ├── Argos translation (all published articles)
                                  └── Azure translation (top clusters, quality tier)
```

This inherits V2 §A3's most important property: **if translation fails entirely, the site still works.** Readers on a non-English setting fall back to original-language text with a visible "not yet translated" marker rather than an empty page. Degraded, not down.

## B6. RTL is a Phase 4 blocker, not a Phase 6 nicety

Arabic, Hebrew, Urdu, Persian. V2 §13 does not mention text direction once.

This has to be right from the first line of CSS:

- Every layout uses **logical properties** — `ms-`/`me-`, `ps-`/`pe-`, `start`/`end` — never `ml-`/`mr-`/`left`/`right`.
- `<html dir>` is set per-request from `languages.direction`.
- Icons with direction semantics (back arrows, "read more →") mirror; logos and play buttons do not.
- Numerals, dates and the publisher-attribution furniture in V2 §13 need RTL review — small-caps letterspacing behaves differently.

Retrofitting RTL into a built frontend is one of the genuinely miserable jobs in web development. Doing it from the start costs close to nothing. **This is the single highest-leverage item in this document.**

## B7. Routing and SEO

Language belongs in the path, not a query parameter:

```
/hi/in/business          ✅  indexable, shareable, cacheable
/in/business?lang=hi     ❌  Google treats as duplicate of the English page
```

Required alongside it:

- `hreflang` alternates on every page, including `x-default`
- `<html lang>` and `dir` set correctly
- Per-language sitemaps, segmented by country as V2 already specifies
- Canonical still points to **the publisher**, per V2 §11 — translation does not change attribution

Scale check: 12 countries × 10 categories × 4 languages = 480 ISR pages on a 300s revalidate. Comfortably within Vercel's free tier, but worth watching if either dimension grows.

## B8. Legal: translation is a derivative work

V2 §11's metadata-only posture did not contemplate translation. A translated headline is a derivative work in a way a verbatim headline is not.

It is defensible on the same footing as the AI summaries V2 already generates — transformative, attributed, linking to the original — but the model should be explicit rather than implicit. Therefore `source_permissions` gains:

```
can_translate BOOL NOT NULL DEFAULT true
```

Default `true` because translation is closer to summarisation (already allowed) than to reproduction (already denied). A publisher who objects gets a single flag flipped, not a code deployment.

Translated pages must carry a visible machine-translation notice with a link to the original. This is both honest and legally useful.

## B9. Storage

UTF-8 makes non-Latin scripts heavier than they look — Hindi and Arabic run 2–3 bytes per character.

| Policy | Per language/month |
|---|---|
| Translate everything | ~110 MB |
| Translate top clusters only | **~5 MB** |

Against V2's 0.5 GB ceiling and finding A6 (fills in 26 days), the first policy is self-defeating. **Argos translates all published articles; only cluster-level translations are retained long-term.** Article-level translations inherit the article's retention window and are deleted by the same nightly job.

## B10. Launch languages

English, Hindi, Spanish, Arabic, French, Portuguese. Chosen for coverage-per-language against the V2 country list, and because they force RTL (Arabic) into scope at launch rather than as a later retrofit — which, per B6, is exactly when it is cheap.

---

# PART C — The category taxonomy

V2 §3.3 designed `category_defs` with a self-referencing `parent_id` but never specified the actual tree. The current code has seven hardcoded strings in `settings.py:VALID_CATEGORIES`, and "AI" is not among them.

Seed taxonomy — 10 top-level, 38 children:

| Top level | Children |
|---|---|
| **World** | conflict, diplomacy, migration, disasters |
| **Politics** | elections, policy, government, law |
| **Business** | economy, markets, companies, startups, jobs, trade |
| **Technology** | **ai**, software, hardware, cybersecurity, consumer-tech, crypto, telecom |
| **Science** | space, research, physics, biology |
| **Health** | public-health, medicine, mental-health |
| **Sports** | football, cricket, tennis, motorsport, olympics, athletics |
| **Culture** | film, music, books, art, food |
| **Environment** | climate, energy, wildlife, pollution |
| **Society** | education, crime, religion, human-rights |

Notes:

- **AI sits under Technology.** It is where readers look for it, and the parent relationship means an AI article automatically appears in Technology feeds without a second assignment.
- The taxonomy is **seeded, not hardcoded** — it lives in `category_defs` rows, editable in the admin panel per V2 §14, exactly as the original brief required.
- `article_categories` is many-to-many with `is_primary`, so a story about an AI company's IPO is legitimately both `ai` and `markets`.
- Classification assigns the **most specific** matching category and the parent is implied by the tree — never stored twice.
- `settings.VALID_CATEGORIES` is deleted in Phase 1. It is a migration artefact, not configuration.

---

# PART D — Country selection

**Decision (19 Aug): inclusion list only.** The reader selects the countries they want. Countries added to the platform later do not silently appear in an existing reader's feed.

This is what `user_followed_countries` in V2 §3.8 already models, and what `localStorage` holds in Phases 1–4, so **no schema change is required.** The exclusion-list variant considered on review is dropped.

`GET /api/v1/feed?countries=in,us,jp&categories=ai,markets&lang=hi` remains stateless, per V2 §12.

One consequence to build for: arbitrary country/category/language combinations cannot be ISR-cached — the combinatorics are unbounded. `/my-feed` is therefore client-rendered and is **the only route that hits the API live on every load**, which makes it the one page that can actually load the database. It gets a short-TTL edge cache and is the first candidate for optimisation if traffic ever becomes real.

---

# PART E — Breaking news

**The gap:** V2 ranks clusters by `source_count` and recency. A genuine breaking story has *one* source and is *three minutes old*, so under the current formula it ranks **last**. That is precisely backwards, and it is the failure mode a news site can least afford.

## E1. Velocity as the missing signal

Absolute source count measures how *established* a story is. Breaking news needs the derivative — how fast coverage is *accumulating*.

```
velocity = articles_added_last_60min / max(1, hours_since_first_seen)
```

A story going from 0 to 3 sources in twenty minutes has far higher velocity than one sitting at 12 sources over two days, and it is the one that belongs at the top.

## E2. Revised cluster score

```
score = w1 · log(1 + source_count)          -- corroboration
      + w2 · velocity                        -- acceleration      ← new
      + w3 · exp(-age_hours / 6)             -- recency, 6h half-life
      + w4 · max(source.editorial_weight)    -- one wire story ≠ one blog post
      + breaking_boost                       -- see E3
```

`editorial_weight` already exists on `sources` in V2 §3.7 for canonical-article selection; this reuses it. Weights live in config and are tunable without a deploy.

## E3. Cheap signals worth taking

- Many feeds prefix items `BREAKING:`, `LIVE:`, `JUST IN:` or set a matching RSS category. A regex over the title is close to free and surprisingly precise.
- `is_breaking BOOL` on `story_clusters`, set when `velocity > threshold AND age < 6h`, cleared automatically when it decays. Drives the live indicator in the UI.
- Breaking status must **expire on its own**. A "BREAKING" badge on a nine-hour-old story destroys more trust than it ever earned.

## E4. Schema delta

```
story_clusters  += velocity FLOAT DEFAULT 0
                += is_breaking BOOL DEFAULT false
                += breaking_since TIMESTAMPTZ NULL
story_cluster_articles already has added_at — velocity needs no new table
```

---

# PART F — The rest of a news website

Things every news site has that V2 does not mention. Ordered by what hurts most to omit.

**F1. Accessibility.** Not mentioned once in V2 §13. The European Accessibility Act has been enforceable since June 2025 and you have EU sources, therefore EU readers. Concretely: WCAG 2.2 AA as the target, semantic landmarks, keyboard-navigable country and language switchers, visible focus states, `prefers-reduced-motion` honoured (V2's near-zero-motion brief makes this nearly free), and real contrast checks on `#111`/`#FAF9F7` and its dark counterpart. Cheap now. Expensive and legally exposed later.

**F2. Dark mode.** Expected by default in 2026. V2's palette has no dark counterpart and inverting it produces exactly the muddy grey the design brief exists to prevent. Design it as a real second palette with its own contrast validation, driven by `prefers-color-scheme` with a manual override.

**F3. About / methodology / corrections.** For an aggregator this is trust infrastructure and legal cover simultaneously. It should state plainly: what the site does, that summaries and translations are machine-generated, how sources are selected, how classification works and that it errs, and how to request a correction or takedown. V2 §11 commits to a 24-hour takedown process; it needs a page a publisher can actually find.

**F4. Privacy policy and cookie handling.** You are in India, so DPDP applies; EU readers mean GDPR. `localStorage` follows are functional and defensible without consent. Any analytics is not. Decide before launch, not after.

**F5. Empty and error states.** ISR on a country with zero articles today renders a blank page. Every country page needs a designed empty state, and every feed needs a stale-data indicator for when ingestion has not run. This is not polish — it is the single most likely thing a real visitor sees in month one.

**F6. Trending / most-read.** Needs only a counter table and a materialised view refreshed hourly. No third-party analytics, no privacy exposure.

**F7. Reading furniture.** Share links, related articles from the same cluster, more-from-this-publisher. Mostly free given the cluster model already exists.

**F8. Time handling.** Relative timestamps ("2h ago") rendered client-side from UTC, with the absolute time in a `title` attribute. Getting this wrong across timezones is a small bug that makes a news site feel broken.

**F9. A classifier gold set.** V2 §A10 correctly calls country classification the thing this product will be judged on, and §8 designs five tiers to get it right — but there is no labelled evaluation set, so "~85% high confidence" is unfalsifiable and the margin thresholds are unmeasurable. **Hand-label 200 articles in Phase 1.** Without it there is no way to know whether a tuning change helped or hurt.

---

# PART G — Revised roadmap

Changes from V2 §15: Supabase moves into Phase 0; language work threads through Phases 1–4; Phase 4 splits in two.

### Phase 0 — Stop the bleeding (1 day) ← **in progress**
- Alembic initialised, baseline stamped
- Groq models replaced; `LLMProvider` abstraction added (A1)
- `url_hash` added, broken unique index dropped (V2 A4)
- robots.txt fallback inverted to fail-closed (V2 A9)
- CORS wildcard-plus-credentials fixed
- Dead Reuters and RSSHub/AP sources removed (V2 A8)
- **Supabase project created, `DATABASE_URL` swapped** — moved up from Phase 2, since there is no production data to preserve and it removes the largest open dependency

### Phase 1 — Geography, taxonomy, permissions (4–5 days)
V2 Phase 1, plus: `languages` table seeded, language detection at ingest, the Part C taxonomy seeded, `can_translate` on `source_permissions`, and the 200-article gold set.

### Phase 2 — Pipeline relocation (3–4 days)
V2 Phase 2, plus: Argos integrated into the runner alongside MiniLM, `article_translations` populated, model cache wired to `actions/cache`.

### Phase 3 — Clustering, ranking, API (4–5 days)
V2 Phase 3, plus: the Part E velocity ranking and `is_breaking`, and `lang` on every API endpoint.

### Phase 4a — Public frontend (6–8 days)
Homepage, country, category, story, article, search. **RTL-correct from the first commit.** i18n routing, `hreflang`, language switcher, dark mode, accessibility, empty states, the F3/F4 static pages. Ships the product.

### Phase 4b — Admin panel (4–5 days)
V2 §14 in full, plus language and translation-tier management and the classification review queue.

### Phase 5 — Enrichment and accounts
V2 Phase 5, plus Azure quality-tier translation and translation-quality review in admin.

### Phase 6 — Expansion
V2 Phase 6, plus non-English gazetteers — which is what unlocks genuine non-English *classification*, as opposed to the translation of English-classified articles that v3 delivers.

**Revised estimate to a live product (Phases 0–4a): 18–23 working days.** V2 said 14–19 for a smaller scope. For a single developer this is realistically 5–7 calendar weeks. The original estimate assumed no debugging and no source archaeology; both will happen.

---

# PART H — Open decisions

| # | Decision | Needed by | Recommendation |
|---|---|---|---|
| 1 | Supabase account created, `DATABASE_URL` issued | **Phase 0** | Supabase — Neon's 100 compute-hours is a hard wall |
| 2 | Repo made public | Phase 2 | Public. Unlimited Actions minutes is load-bearing |
| 3 | Azure account for the translation quality tier | Phase 5 | F0 tier, no card, hard-stops at quota rather than billing |
| 4 | Gemini API key | Phase 5 | Free from AI Studio; note the free-tier training term |
| 5 | Confirm launch languages | Phase 1 | en, hi, es, ar, fr, pt (B10) |
| 6 | Confirm the 12 launch countries | Phase 1 | V2's list stands |

---

## Changelog

**v3.0 — 19 Aug 2026**
- Groq deprecation corrected to actual (A1); LLM budget restated (A2)
- Language and translation designed end-to-end (Part B) — two-tier Argos + Azure, RTL, i18n routing, derivative-work posture
- Category taxonomy specified, AI included (Part C)
- Country selection settled as inclusion-list; no schema change (Part D)
- Breaking-news velocity ranking added (Part E)
- Eight missing news-site concerns specified (Part F)
- Roadmap revised; Supabase moved to Phase 0; Phase 4 split (Part G)
