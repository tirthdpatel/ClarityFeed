# ClarityFeed v2 — Architecture & Design Review

**Status:** Design proposal. No implementation code has been written. Awaiting approval.
**Date:** 9 August 2026
**Scope:** Evolve the existing Phase 1/2 backend into the country-centric global news product described in the build brief.

---

## 0. What I actually read before designing this

This is not a greenfield project. The repo already contains a working, tested Phase 1 + Phase 2 backend:

| Component | File | Lines | State |
|---|---|---|---|
| Compliance layer (robots.txt, rate limit, ETag, attribution) | `backend/compliance.py` | 218 | Working, wired into both fetchers |
| RSS fetcher | `backend/collector/rss_fetcher.py` | 158 | Working |
| Article body fetcher (newspaper3k → readability → BS4) | `backend/fetcher/article_fetcher.py` | 238 | Working |
| Text cleaner (7 transforms) | `backend/cleaner/text_cleaner.py` | 140 | Working |
| Deduplicator (HF embeddings + cosine) | `backend/deduplicator/` | 322 | Working |
| Summarizer (Groq) | `backend/summarizer/` | 260 | Working |
| Categorizer (Groq zero-shot, 7 labels) | `backend/categorizer/categorizer.py` | 156 | Working |
| ORM schema (6 tables) | `backend/database/orm_models.py` | 189 | Working |
| Public API | `backend/api/main.py` | 112 | Only `/health` + `/sources` |
| Tests | `tests/unit/` | ~1,500 | 12 test modules |
| Frontend | `frontend/` | 0 | **Empty** |

**The plan below preserves all of this.** Nothing is thrown away. The work is: fix what is factually broken, add the country/cluster/user model via Alembic, replace the LLM strategy, and build the frontend.

**Decisions you locked in:**

1. **Metadata-only by default.** Titles, descriptions, links. Full article text only where a source explicitly permits it. Summaries are acceptable.
2. **Next.js on Vercel** for the frontend.
3. **Alembic incremental migrations.** Preserve existing data and keep the ingestion pipeline working throughout.

---

# PART A — Adversarial review

You asked me not to agree with you and not to hide problems. Here are ten findings. Six are in the code that already exists; four are in the brief itself. Each has a fix, and the fixes are folded into the design in Part B.

---

### A1. Your Groq budget is off by 6×, and both configured models are deprecated

**The problem.** `docs/phase2_pipeline_guide.md` budgets ~5,760 Groq calls/day against a stated free limit of 14,400/day. Groq's free tier is now **30 RPM / 6,000 TPM / 1,000 requests per day** for most models — requests-per-day is the binding constraint, and it is roughly 6× smaller than the pipeline assumes. Separately, `config/settings.py` sets `GROQ_MODEL_PRIMARY = "llama-3.1-8b-instant"` and `GROQ_MODEL_FALLBACK = "mixtral-8x7b-32768"`. Mixtral 8x7B was deprecated by Groq in March 2025; llama-3.1-8b-instant was announced for deprecation in June 2026. **Both models in your config are on the way out.**

**Why it matters.** The pipeline makes two LLM calls per article (summarize + categorize). At 50 articles/cycle × 96 cycles/day that is 9,600 calls against a 1,000/day ceiling. In production the pipeline would run for roughly the first 25 minutes of each day and then fail for the remaining 23.5 hours. Every article after that point gets stuck in `PENDING` forever, because `PROCESSED` is only set by the categorizer.

**The fix.** Three changes, in order of impact:

1. **Stop using an LLM for classification.** Category and country are deterministic enough for rules. A weighted keyword + gazetteer classifier costs zero API calls and handles ~85% of articles with high confidence. This alone removes half the call volume.
2. **Batch what remains.** Send 20 articles in one prompt and get back a JSON array. 30 articles/cycle becomes 2 calls instead of 30.
3. **Summarize only what gets read.** Do not summarize every article. Rank clusters by source count and recency, and summarize the top ~40/day. Summaries on page 9 of the Brazil feed are wasted quota.

Net effect: from ~9,600 calls/day to roughly **150–200 calls/day**, comfortably inside any free tier, with headroom to survive a provider outage.

---

### A2. Answering your question directly — Groq or Gemini?

You asked: *"is the groq key free? should I use gemini key?"*

**Both are free, neither is generous enough to matter if the pipeline stays as-is.** Current free-tier ceilings:

| Provider / model | RPM | Requests/day | Notes |
|---|---|---|---|
| Groq (most models) | 30 | ~1,000 | llama-3.1-8b-instant + mixtral-8x7b both deprecated |
| Gemini 2.5 Flash-Lite | 15 | 1,000 | Current, actively supported |
| Gemini 2.5 Flash | 10 | 250 | |
| Gemini 2.5 Pro | 5 | 100 | |

**Recommendation: use Gemini 2.5 Flash-Lite as primary, keep Groq as automatic fallback, and put both behind one interface.** Reasons:

- Flash-Lite gives you the same 1,000 RPD as Groq but on a **current, supported model** rather than two deprecated ones.
- Running both doubles your effective daily quota for free, and gives you an outage escape hatch. Groq and Google will not go down on the same day.
- The abstraction costs about 60 lines. Hard-coding one provider is how you end up rewriting the summarizer in six months.

**Two caveats you should know before choosing:**

- **Gemini free-tier inputs and outputs may be used by Google to train its models.** For ClarityFeed this is low-risk — you are sending public news headlines, not private data — but it is a real term, and it does *not* apply to Gemini's paid tier. If you ever process user data through the same key, revisit this.
- **Google cut free-tier quotas by 50–80% in December 2025 without notice.** Treat any free quota as something that can halve overnight. This is precisely why the design must not depend on the LLM being available — see A3.

**The design consequence:** the pipeline must produce a complete, correct, browsable site with **zero LLM calls**. AI summaries are a garnish on top. Your brief already says this ("The system should still function without AI") — I am reinforcing it, because the current code violates it: `PROCESSED` status is only ever set by the Groq categorizer, so an LLM outage today means an empty website.

---

### A3. The LLM is a hard dependency on the critical path

**The problem.** In `backend/collector/pipeline.py` the stage order is fetch → clean → dedup → summarize → categorize, and `raw_articles.status` only becomes `PROCESSED` in the categorizer. The API is specified to serve only `PROCESSED` articles.

**Why it matters.** Groq rate-limits you, or Google halves the free tier, or the model name 404s after deprecation — and the site shows nothing. Not degraded: **empty**. A news site that goes blank when a third-party AI vendor has a bad afternoon is not a news site.

**The fix.** Split the status into two independent axes:

- `ingest_status`: `PENDING → NORMALIZED → PUBLISHED | FAILED`. Set by deterministic code only. An article becomes `PUBLISHED` as soon as it has a title, URL, publisher, and a country assignment. No AI involved.
- `enrichment_status`: `NONE | SUMMARIZED | CLASSIFIED_BY_LLM`. Purely additive. Never gates display.

The API serves on `ingest_status = PUBLISHED`. AI enrichment upgrades articles in place, asynchronously, best-effort. Turn off every API key and the site still works — it just shows headlines and rule-based categories instead of summaries.

---

### A4. `url = Column(String(2048), unique=True)` will throw a runtime error in Postgres

**The problem.** `RawArticle.url` is `String(2048)` with `unique=True`, which creates a btree index. Postgres btree index entries are capped at roughly **2,704 bytes** (one third of an 8 KB page). A 2,048-character URL containing non-ASCII characters — common for Hindi, Japanese, Arabic, or Russian publishers, exactly the sources this product needs — encodes to up to 8,192 bytes in UTF-8.

**Why it matters.** The insert fails with `index row size ... exceeds btree version 4 maximum`. It will not surface in local SQLite testing, and it will not surface with the ten English seed sources. It surfaces the day you add a Hindi or Japanese publisher, as a hard insert failure on a subset of articles that is annoying to diagnose. The same latent bug exists on `Source.feed_url`.

**The fix.** Add a `url_hash CHAR(64)` column holding `sha256(canonical_url)` and put the unique index there. Keep `url` as an unindexed `Text` column. Do the same for `feed_url`. Canonicalize before hashing: lowercase host, strip `utm_*`/`fbclid`/`gclid`, drop trailing slash, drop fragment. This also fixes a second problem — right now the same article arriving with and without a UTM tag is treated as two distinct articles.

---

### A5. pgvector is free on Neon. The JSON-text workaround is both unnecessary and your biggest storage cost.

**The problem.** `backend/database/orm_models.py` documents: *"Embeddings are stored as JSON-serialized Text, not pgvector. pgvector requires the Neon Pro plan."* **This is incorrect.** pgvector is a standard Postgres extension and is available on Neon's free plan.

**Why it matters.** A 384-dimensional float32 vector serialized as JSON text is roughly **3.5 KB** per article (each float renders as ~9 characters plus a comma). As a `pgvector` `halfvec(384)` it is **768 bytes** — a 4.5× reduction. Right now the embedding is the single largest per-article storage consumer, on a 0.5 GB database. Worse, dedup currently deserializes 100 rows of JSON into Python and runs scikit-learn cosine similarity in a 512 MB Render process, when Postgres could do it in the index.

**The fix.** `CREATE EXTENSION vector;` then store `halfvec(384)` with an HNSW index. Similarity search becomes a SQL `ORDER BY embedding <=> $1 LIMIT 20`, which is faster, uses no application RAM, and scales past the arbitrary 100-article window. This also removes the `scikit-learn` + `numpy` dependency (~100 MB of the Render build).

---

### A6. Storage: you hit the 0.5 GB wall in ~26 days, and there is no cleanup job

**The problem.** Your own Phase 2 doc computes ~19 MB/day growth and notes the free tier fills in about 26 days. The recommended cleanup job was deferred to "Phase 3" and does not exist.

**Why it matters.** When Neon hits 0.5 GB, writes fail. The pipeline starts erroring, and because there is no source-health alerting yet, the first symptom you notice is that the site stopped updating.

**The fix.** Four things, which together cut growth by roughly 20×:

1. **Metadata-only** (your decision) removes `cleaned_articles.clean_text` for the vast majority of sources — the single biggest saving.
2. **halfvec embeddings** (A5): 3.5 KB → 768 bytes.
3. **Retention job, built in Phase 1, not deferred.** Embeddings are only needed for the 72-hour clustering window — delete them after 7 days. Articles older than 90 days get trimmed to title/url/publisher/date and unlink from clusters.
4. **Hard storage guard.** A pre-ingestion check that queries `pg_database_size()` and refuses to ingest above 85% capacity, logging loudly. Fail visibly, not silently.

Revised estimate: ~1 MB/day steady-state, with retention holding the total under 200 MB indefinitely.

---

### A7. Neon's real binding constraint is 100 compute-hours/month, not 0.5 GB — and your cron blows through it 3×

**The problem.** Neon's free plan meters **100 compute-hours per project per month**. Neon auto-suspends after 5 minutes of inactivity. Your GitHub Actions cron fires **every 15 minutes**, 96 times a day. Each run wakes the database and keeps it awake for the pipeline duration plus the 5-minute suspend timer.

Conservatively: 96 runs × (2 min pipeline + 5 min idle) = **11.2 hours/day = ~336 compute-hours/month**. That is **3.4× over the free limit**, and it ignores the compute consumed by actual site visitors.

**Why it matters.** This is not a slow degradation. You exhaust the month's compute in roughly the first nine days, and the project is throttled or suspended for the remaining three weeks. It is the single most likely reason this project silently dies in production, and nothing in the current repo accounts for it.

**The fix — and this is a real architectural fork:**

- **Option 1 (recommended): move to Supabase.** Supabase's free tier is 500 MB Postgres with **no compute-hour metering**; projects only pause after a full week of *zero* activity, which an hourly ingest prevents entirely. Same Postgres, same pgvector, same SQLAlchemy code — the migration is a connection-string change plus running the Alembic head. This removes the constraint rather than working around it.
- **Option 2: stay on Neon and drop the cadence to hourly.** 24 runs × 7 min = 2.8 h/day ≈ 84 h/month. Under 100, but with only 16% headroom for all user traffic combined. Workable, permanently tight.

Either way: **15-minute ingestion is not justified.** News that matters is picked up within an hour by a personal aggregator, and the cost of that cadence is your entire compute budget. Hourly ingest, with an on-demand manual trigger in the admin panel for breaking events.

---

### A8. Two of your ten seed sources are dead or third-party proxies

**The problem.** In `backend/collector/feed_sources.py`:

- **Reuters** — `reutersagency.com/feed/?taxonomy=best-topics` — Reuters discontinued its public RSS feeds. This endpoint does not serve articles.
- **Associated Press** — `rsshub.app/apnews/topics/apf-topnews` — this is not AP. It is **RSHub**, a third-party open-source scraper running on a shared public instance. It is heavily rate-limited, frequently down, and you would be depending on someone else's scraper for AP content — with all of AP's terms applying to you and none of RSSHub's reliability guarantees.

**Why it matters.** Two of the three highest-authority wire services in your seed list produce nothing. Because failures are logged and swallowed, the site would quietly under-cover the US and international beats and you would have no signal that anything was wrong.

**The fix.** Drop both from the seed list. Replace with publishers that operate their own feeds (AP, Reuters, and AFP all require paid licensing for programmatic access — treat wire content as out of scope for the free tier and say so honestly). Build the **source health monitor in Phase 1**, not Phase 4: track `consecutive_failures`, auto-disable a source after N consecutive zero-article fetches, and surface it in the admin dashboard. A feed returning HTTP 200 with zero entries must be treated as a failure, not a success — that is how dead feeds hide.

---

### A9. robots.txt compliance is not copyright compliance — and the fallback is backwards

**The problem.** `backend/compliance.py` is genuinely good work: robots.txt checking, per-domain token buckets, ETag/Last-Modified conditional requests, attribution validation, all correctly wired into both `rss_fetcher.py` and `article_fetcher.py`. That is more compliance rigour than most projects of this kind. But two things are wrong:

1. **`RobotsTxtChecker.is_allowed()` returns `True` when robots.txt cannot be fetched.** The comment calls this a "permissive fallback." If a publisher's server is unreachable, misconfigured, or actively blocking you, the code interprets that as consent.
2. **There is no content-permission model at all.** `article_fetcher.py` scrapes and stores full body text for *every* source, unconditionally. Your brief's central legal requirement — the per-source `can_store_full_text` flag — does not exist anywhere in the codebase.

**Why it matters.** robots.txt governs *crawling*. Copyright governs *copying, storing, and republishing*. They are unrelated legal questions. A permissive `robots.txt` is not a licence to store a publisher's article body on your server. This is the gap between "technically retrieved it" and "allowed to keep it," and it is the specific risk you told me you want to avoid.

**The fix.**

1. **Invert the fallback: fail closed.** Unreachable robots.txt means do not fetch. Add an explicit per-source `robots_override` boolean that an admin sets deliberately after reading the site's terms, so the permissive path requires a human decision that is recorded with a timestamp and a reason.
2. **Build the `source_permissions` table before any new ingestion code**, defaulting every field to the restrictive value. `can_store_full_text` defaults to `false`. The article body fetcher does not run unless that flag is explicitly `true` for that source, set by you, with a `terms_url` and `reviewed_at` recorded alongside it.
3. **Enforce it at the API layer too**, not only at ingestion. The serializer refuses to emit `content` unless the source's permission row allows it, so a bug in ingestion cannot become a publishing incident.

---

### A10. Country classification is the hardest correctness problem in this product, and it is the one you will be judged on

**The problem.** Getting an article filed under the wrong country is the most visible possible failure of a country-first news app. And country names are riddled with ambiguity:

| Token | Ambiguity |
|---|---|
| Georgia | Country vs. US state |
| Jordan | Country vs. very common surname |
| Turkey | Country vs. bird vs. Thanksgiving |
| China | Country vs. porcelain |
| Chad, Niger, Guinea | Countries vs. names, and Guinea vs. Guinea-Bissau vs. Equatorial Guinea vs. Papua New Guinea |
| Sydney, Victoria, Alexandria | Cities vs. given names |
| "Washington", "Moscow", "Paris" | Capitals vs. US towns (Paris, Texas; Moscow, Idaho) |

Naive substring matching produces "Michael Jordan retires" filed under Jordan. Once, that is funny. On a homepage, it destroys trust.

**Why it matters.** Your brief lists "Incorrect country classification" as a risk but proposes no mechanism. Every other feature — country feeds, personalized feeds, the country selector, international detection — is built on top of this one signal being right.

**The fix.** A five-tier cascade where each tier can only run if the previous one was inconclusive, described fully in §8. The essential points:

- **Source priors do most of the work for free.** A Times of India article is about India unless proven otherwise. This alone is ~70% accurate with zero computation.
- **Weighted alias gazetteer, not substring matching.** Demonyms ("Brazilian") and capitals ("Brasília") are far stronger signals than bare country names. Store weights per alias.
- **An explicit ambiguity list.** Aliases flagged `is_ambiguous` require a corroborating second signal in the same text before they can score. "Georgia" only counts if "Tbilisi", "Caucasus", or "Georgian" also appears.
- **Confidence and margin thresholds.** Assign a primary country only when the top score clears an absolute threshold *and* beats the runner-up by a margin. Otherwise fall back to the source's country and mark it low-confidence.
- **Store the method and confidence on every assignment**, so the admin panel can show you a queue of low-confidence classifications to review, and so you can re-run the classifier over historical articles when you improve it.

This is the part of the system worth building carefully. Everything else is plumbing.

---

## Summary of what changes

| # | Finding | Severity | Where fixed |
|---|---|---|---|
| A1 | Groq budget 6× over; both models deprecated | **Critical** | §8, §5 |
| A2 | Provider choice: Gemini primary, Groq fallback | High | §2 |
| A3 | LLM on the critical path; site empties on outage | **Critical** | §7, §5 |
| A4 | Unique index on 2048-char URL will error in Postgres | **Critical** | §3 |
| A5 | pgvector wrongly believed paid; 4.5× storage waste | High | §3, §9 |
| A6 | Fills 0.5 GB in 26 days; no cleanup job | **Critical** | §3, §5 |
| A7 | Neon 100 compute-h/month exceeded 3.4× by cron | **Critical** | §2, §5 |
| A8 | Reuters feed dead; AP via third-party scraper | High | §6, §14 |
| A9 | robots.txt fails open; no content-permission model | **Critical** | §10, §11 |
| A10 | Country classification ambiguity unaddressed | **Critical** | §8 |

---

# PART B — The design

## 1. Complete architecture

The single most important change is **moving ingestion out of the web service entirely**. Today, GitHub Actions pokes a sleeping Render instance and Render does the work inside a 512 MB container. Instead, GitHub Actions *is* the worker.

```
   ┌──────────────────────── INGESTION (GitHub Actions, hourly) ────────────────────────┐
   │                                                                                     │
   │   Source registry (DB)                                                              │
   │          │                                                                          │
   │          ├─── APIAdapter ──────┐                                                    │
   │          ├─── RSSAdapter ──────┤   each adapter emits NormalizedArticle             │
   │          └─── ScrapeAdapter ───┘   (permission-gated, robots-checked, rate-limited) │
   │                    ↓                                                                │
   │            Normalization  ──── canonical URL, url_hash, language detect             │
   │                    ↓                                                                │
   │            Permission gate ──── drops fields the source does not licence            │
   │                    ↓                                                                │
   │            Classification ──── country / countries / category / region  (RULES)     │
   │                    ↓                                                                │
   │            Embedding      ──── local MiniLM in the runner (free, no API quota)      │
   │                    ↓                                                                │
   │            Clustering     ──── blocking → pgvector ANN → single-link assignment     │
   │                    ↓                                                                │
   │            PUBLISH        ──── ingest_status = PUBLISHED   ← site is live from here │
   │                    ↓                                                                │
   │            Enrichment     ──── LLM summaries for top-ranked clusters only           │
   │                                (best-effort, capped, never blocks publish)          │
   └─────────────────────────────────────┬───────────────────────────────────────────────┘
                                         ↓
                          ┌──────────────────────────────┐
                          │  Postgres + pgvector          │
                          │  (Supabase free / Neon free)  │
                          └───────────────┬──────────────┘
                                          ↓
                          ┌──────────────────────────────┐
                          │  FastAPI read API (Render)    │
                          │  /api/*  — cached, read-only  │
                          │  /admin/* — authenticated     │
                          └───────────────┬──────────────┘
                                          ↓
                          ┌──────────────────────────────┐
                          │  Next.js on Vercel            │
                          │  SSR + ISR, revalidate 300s   │
                          │  → shields DB from traffic    │
                          └──────────────────────────────┘
```

**Why ingestion belongs in GitHub Actions:**

| | Render free web service | GitHub Actions runner |
|---|---|---|
| RAM | 512 MB | 7 GB |
| Cold start | 30–60 s before work begins | none |
| Can run sentence-transformers locally | No — OOM | Yes, comfortably |
| Timeout | HTTP request bound | 6 hours |
| Cost (public repo) | free, 750 h/mo | free, unlimited minutes |

This single move eliminates three problems at once: the HuggingFace API dependency and its quota (embeddings run locally in the runner), the 512 MB memory ceiling that forced the JSON-text embedding workaround, and the cold-start-mid-pipeline failure mode. It also deletes the `/internal/collect` webhook and its shared-secret surface entirely.

**One caveat to plan for:** GitHub disables scheduled workflows in public repositories after 60 days with no commit activity. The workflow should include a step that touches a `.last-run` file and commits it weekly, or you accept a monthly manual re-enable.

**Reads never touch Postgres on the hot path.** Next.js renders country and article pages with ISR (`revalidate: 300`). A country page is regenerated at most 12 times an hour regardless of whether it gets 10 visitors or 100,000. Vercel's edge serves the rest. This is what makes the free database tier survivable.

---

## 2. Technology stack

| Layer | Choice | Why this and not the alternative |
|---|---|---|
| Ingestion runtime | **Python 3.11 in GitHub Actions** | Reuses ~2,500 lines of tested code. 7 GB RAM, no cold start, free on public repos. |
| Read API | **FastAPI on Render free** | Already built. Read-only and cacheable, so cold starts only affect ISR regeneration, never a user. |
| Database | **Postgres + pgvector.** Supabase free (recommended) or Neon free | See A7 — Neon's 100 compute-hours/month is the binding constraint, not storage. Supabase does not meter compute. Identical code either way. |
| ORM / migrations | **SQLAlchemy 2.0 + Alembic** | Already in use; Alembic is your stated requirement. |
| Embeddings | **`sentence-transformers/all-MiniLM-L6-v2`, run locally in the runner** | Removes the HuggingFace API dependency and its quota entirely. The Phase 2 doc excluded torch because of Render's 512 MB limit — that limit does not apply in Actions. |
| Vector search | **pgvector `halfvec(384)` + HNSW** | Free on both providers. 4.5× smaller than JSON text, and search happens in the DB instead of in application RAM. |
| LLM | **Gemini 2.5 Flash-Lite primary, Groq fallback, behind a `SummaryProvider` interface** | See A2. Current supported models, doubled effective quota, outage tolerance. |
| Frontend | **Next.js 15 App Router on Vercel** | Your choice. SSR/ISR gives real SEO — non-negotiable for news — and shields the DB. |
| Styling | **Tailwind + a deliberate editorial type scale** | See §13 on avoiding the generic look. |
| Search | **Postgres full-text (`tsvector` + GIN) + `pg_trgm`** | Zero extra infrastructure. Meilisearch/Typesense add a service to host; revisit past ~500k articles. |
| Auth | **Deferred to Phase 5.** Follows stored in `localStorage` first | Anonymous personalization covers 90% of the value at 0% of the security surface. See §10. |
| Scheduling | **GitHub Actions cron, hourly** | See A7. |
| Monitoring | **Actions run summaries + a `source_health` table surfaced in admin** | Free. No Sentry/Datadog needed at this scale. |

**Total recurring cost: $0.** The only paid path this design consciously declines is wire-service licensing (AP/Reuters/AFP), which is genuinely unavailable for free — see §14.

---

## 3. Database schema

Two design rules, both from your brief: *"Do not create an unnecessarily complicated database"* and *"Explain the relationships before implementing them."*

### 3.1 What already exists and is kept

`sources`, `raw_articles`, `cleaned_articles`, `embeddings`, `summaries`, `categories`. All six survive. `raw_articles` is renamed conceptually to "the article table" but keeps its physical name so the existing pipeline keeps working during migration.

### 3.2 Geography

```
regions                 id, code, name, sort_order
countries               id, iso2 (unique), iso3, name, slug (unique), region_id → regions,
                        default_language, flag_emoji, is_enabled, sort_order,
                        cached_article_count, cached_at
country_aliases         id, country_id → countries, alias (indexed, lowercased),
                        alias_type ENUM(name|demonym|capital|city|adjective|leader|iso|currency),
                        weight FLOAT, is_ambiguous BOOL, requires_context TEXT[]
```

`country_aliases` is the engine of §8 and the reason countries are not hard-coded. Adding a country is: one `countries` row plus 15–40 `country_aliases` rows, inserted through the admin panel. No code changes, no redeploy — which is exactly what your brief asked for.

`requires_context` holds the disambiguation terms for `is_ambiguous` aliases. For "Georgia" (country) that array is `{tbilisi, caucasus, georgian, saakashvili, abkhazia}`. If none appear in the text, the alias does not score.

### 3.3 Categories as a real taxonomy

The existing `categories` table is one row *per article* — it is a classification result, not a taxonomy. It gets renamed to `article_classifications` for clarity, and a proper taxonomy is added:

```
category_defs           id, slug (unique), name, parent_id → category_defs (self-FK),
                        description, sort_order, is_enabled, color_token
article_categories      article_id → raw_articles, category_id → category_defs,
                        confidence FLOAT, is_primary BOOL, method ENUM(rule|llm|source|manual)
                        PRIMARY KEY (article_id, category_id)
```

Self-referencing `parent_id` gives you Business → Economy → Markets without a second table. `is_enabled` lets you turn a category off without deleting historical assignments.

### 3.4 Article ↔ country, many-to-many

```
article_countries       article_id → raw_articles, country_id → countries,
                        relevance ENUM(primary|secondary|mentioned),
                        confidence FLOAT, method ENUM(source|section|gazetteer|llm|manual),
                        published_at TIMESTAMPTZ   ← DENORMALIZED, see below
                        PRIMARY KEY (article_id, country_id)
```

**Why `published_at` is denormalized here.** The single hottest query in the product is "latest articles for country X":

```sql
SELECT a.* FROM raw_articles a
JOIN article_countries ac ON ac.article_id = a.id
WHERE ac.country_id = $1 AND a.ingest_status = 'PUBLISHED'
ORDER BY a.published_at DESC LIMIT 30;
```

Without denormalization Postgres must join, then sort — at a million articles that is a heap scan and an external sort on every country page load. With `published_at` copied into `article_countries` and a composite index on `(country_id, relevance, published_at DESC)`, the query becomes an index-only range scan that stops after 30 rows. Constant time regardless of table size. The cost is one extra timestamp per row and keeping it in sync, which is a single trigger. **This is the most important index decision in the schema.**

### 3.5 Story clusters

```
story_clusters          id, canonical_title, slug, summary_id → summaries (nullable),
                        primary_country_id → countries, primary_category_id → category_defs,
                        centroid halfvec(384), first_seen_at, last_seen_at,
                        article_count, source_count, is_international, score FLOAT
story_cluster_articles  cluster_id → story_clusters, article_id → raw_articles,
                        similarity FLOAT, is_canonical BOOL, added_at
                        PRIMARY KEY (cluster_id, article_id)
```

Note `source_count` as a stored column: it is the ranking signal. Five publishers covering one event is the strongest available proxy for "this matters," and computing it per request would be a `COUNT(DISTINCT)` on every homepage render.

### 3.6 Source permissions — the legal core

```
source_permissions      source_id → sources (PK, 1:1)
                        can_store_title           BOOL NOT NULL DEFAULT true
                        can_store_description     BOOL NOT NULL DEFAULT true
                        can_store_full_text       BOOL NOT NULL DEFAULT false   ← deny by default
                        can_store_image           BOOL NOT NULL DEFAULT false   ← deny by default
                        can_generate_summary      BOOL NOT NULL DEFAULT true
                        requires_attribution      BOOL NOT NULL DEFAULT true
                        original_url_required     BOOL NOT NULL DEFAULT true
                        max_description_chars     INT  NOT NULL DEFAULT 300
                        robots_override           BOOL NOT NULL DEFAULT false
                        terms_url, robots_url, licence_note TEXT
                        reviewed_by, reviewed_at, review_notes
```

Every restrictive default is deliberate. A source added without review can contribute a headline, a short excerpt, and a link — nothing more. `reviewed_at` being `NULL` is itself a state the admin panel surfaces: *"14 sources have never been permission-reviewed."*

### 3.7 Columns added to existing tables

**`sources`** gains: `country_id`, `region_id`, `ingestion_method ENUM(api|rss|scrape)`, `endpoint`, `api_key_ref` (an env-var *name*, never a value), `rate_limit_rps`, `is_international`, `editorial_weight` (drives canonical-article selection), `homepage_url`, `logo_url`, `feed_url_hash CHAR(64) UNIQUE`, `last_success_at`, `last_error_at`, `last_error`, `consecutive_failures`.

**`raw_articles`** gains: `url_hash CHAR(64) UNIQUE` (see A4), `canonical_url`, `description`, `image_url`, `author_name`, `language`, `simhash BIGINT`, `primary_country_id`, `is_international`, `cluster_id`, `ingest_status`, `enrichment_status`, `search_vector TSVECTOR`. The existing `url` unique constraint is **dropped** and `url` becomes plain `Text`.

**`embeddings`** gains `vector halfvec(384)`; `vector_json` is backfilled into it and then dropped.

### 3.8 Users — designed now, built in Phase 5

```
users                     id, email (unique, citext), password_hash, display_name,
                          created_at, last_seen_at, is_admin
user_followed_countries   user_id, country_id, sort_order   PK(user_id, country_id)
user_followed_categories  user_id, category_id              PK(user_id, category_id)
user_followed_sources     user_id, source_id                PK(user_id, source_id)
user_hidden_sources       user_id, source_id                PK(user_id, source_id)
user_saved_articles       user_id, article_id, saved_at     PK(user_id, article_id)
user_reading_history      user_id, article_id, read_at      PK(user_id, article_id, read_at)
```

**These tables are created empty in Phase 2 and not used until Phase 5.** Phase 1–4 store follows in `localStorage`. The rationale is in §10: the moment you accept passwords you own a breach surface, and the personalization value is identical without accounts until users want cross-device sync.

`user_reading_history` is the one table that grows without bound. It gets a 90-day retention policy from day one.

### 3.9 Operational tables

```
ingestion_runs      id, started_at, finished_at, trigger ENUM(cron|manual|backfill),
                    status, sources_attempted, sources_ok, sources_failed,
                    articles_new, articles_duplicate, clusters_created,
                    llm_calls_used, error_summary
source_health       id, source_id, checked_at, ok BOOL, http_status, latency_ms,
                    articles_returned, error_type, error_detail
```

`articles_returned` is what catches A8: a feed that answers 200 with zero entries for six consecutive runs is dead, and the auto-disable rule keys off this column rather than off HTTP status.

### 3.10 Tables I am deliberately not building

- **`authors`** — your brief lists it. I recommend a denormalized `author_name` string first. Bylines are wildly inconsistent ("By Jane Doe", "Jane Doe and Reuters staff", "Staff Writer", absent entirely). Entity-resolving them into a table is a project of its own that delivers nothing until you have author pages. Add it in Phase 6 if author following becomes a real feature.
- **`article_media`** — a single `image_url` on the article covers every use case in the brief. A media table is right when you have galleries and video.
- **`ingestion_jobs`** as distinct from `ingestion_runs` — one table, not two.

### 3.11 Index plan

| Index | Purpose |
|---|---|
| `raw_articles (url_hash)` UNIQUE | Dedup on ingest; replaces the broken URL index (A4) |
| `raw_articles (ingest_status, published_at DESC)` | Global feed |
| `article_countries (country_id, relevance, published_at DESC)` | Country feed — the hot path (§3.4) |
| `article_categories (category_id, article_id)` | Category filter |
| `story_clusters (last_seen_at DESC, score DESC)` | Homepage ranking |
| `raw_articles USING GIN (search_vector)` | Full-text search |
| `raw_articles USING GIN (title gin_trgm_ops)` | Fuzzy title match for clustering blocks |
| `embeddings USING hnsw (vector halfvec_cosine_ops)` | ANN similarity |
| `country_aliases (lower(alias))` | Gazetteer lookup |
| `source_health (source_id, checked_at DESC)` | Admin dashboard |

---

## 4. Project structure

Extends what exists rather than reorganizing it. New directories are marked.

```
ClarityFeed/
├── backend/
│   ├── compliance.py                  ← keep; fail-closed fix (A9)
│   ├── sources/                       ← NEW: the plugin layer (§6)
│   │   ├── base.py                    #   SourceAdapter ABC
│   │   ├── registry.py                #   method → adapter resolution
│   │   ├── rss_adapter.py             #   wraps existing rss_fetcher
│   │   ├── api_adapter.py             #   NewsAPI / GDELT / provider APIs
│   │   └── scrape_adapter.py          #   permission + robots gated
│   ├── collector/                     ← keep
│   ├── fetcher/                       ← keep; now permission-gated
│   ├── cleaner/                       ← keep
│   ├── normalize/                     ← NEW
│   │   ├── canonical_url.py           #   canonicalize + sha256 (A4)
│   │   ├── language.py                #   langdetect
│   │   └── permissions.py             #   field stripping per source
│   ├── classify/                      ← NEW: replaces LLM-only categorizer
│   │   ├── gazetteer.py               #   alias loading + matching
│   │   ├── country_classifier.py      #   the 5-tier cascade (§8)
│   │   ├── category_classifier.py     #   weighted keyword rules
│   │   └── llm_adjudicator.py         #   batched, capped, optional
│   ├── cluster/                       ← NEW: replaces deduplicator
│   │   ├── simhash.py
│   │   ├── blocking.py
│   │   └── clusterer.py               #   incremental single-link
│   ├── enrich/                        ← NEW: was summarizer/
│   │   ├── provider.py                #   SummaryProvider interface
│   │   ├── gemini_provider.py
│   │   ├── groq_provider.py           #   existing groq_client, adapted
│   │   └── ranker.py                  #   picks which clusters get summarized
│   ├── retention/                     ← NEW
│   │   └── cleanup.py                 #   A6 — built in Phase 1
│   ├── pipeline/                      ← NEW: orchestration, was collector/pipeline.py
│   │   └── run.py                     #   the GitHub Actions entrypoint
│   ├── api/
│   │   ├── main.py                    ← keep
│   │   ├── public/                    ← NEW: /api/* (§12)
│   │   └── admin/                     ← NEW: /admin/* (§14)
│   └── database/
│       ├── orm_models.py              ← extended
│       └── session.py                 ← keep
├── alembic/                           ← NEW
│   └── versions/                      #   the migration chain (Part C)
├── data/                              ← NEW: seed data, version-controlled
│   ├── countries.yaml                 #   ISO + region + language
│   ├── aliases/                       #   per-country gazetteers
│   ├── categories.yaml
│   └── sources/                       #   one YAML per country
├── web/                               ← NEW: Next.js (replaces empty frontend/)
│   ├── app/
│   │   ├── page.tsx                   #   global homepage
│   │   ├── international/page.tsx
│   │   ├── [country]/page.tsx
│   │   ├── [country]/[category]/page.tsx
│   │   ├── story/[slug]/page.tsx      #   cluster page
│   │   ├── article/[id]/page.tsx
│   │   ├── search/page.tsx
│   │   ├── my-feed/page.tsx
│   │   └── admin/
│   ├── components/
│   └── lib/
├── .github/workflows/
│   ├── ingest.yml                     ← REPLACED collect.yml (runs the pipeline)
│   ├── retention.yml                  ← NEW: nightly cleanup
│   └── test.yml                       ← NEW: CI
└── tests/                             ← keep + extend
```

`architecture.py` and `scaffold.sh` at the repo root are Phase 1 scaffolding artifacts and should be deleted or moved into `docs/`.

---

## 5. News ingestion architecture

One hourly GitHub Actions run, seven stages. **Every stage is idempotent and independently resumable** — a crash in stage 5 does not lose stages 1–4, because each stage commits before the next begins.

| # | Stage | Reads | Writes | Fails how |
|---|---|---|---|---|
| 1 | **Collect** | `sources`, `source_permissions` | `raw_articles` (PENDING), `source_health` | Per-source. One dead feed is logged and skipped. |
| 2 | **Normalize** | PENDING articles | canonical_url, url_hash, language, description | Per-article → FAILED |
| 3 | **Permission gate** | `source_permissions` | strips disallowed fields; decides whether stage 3b runs | Never fails; only restricts |
| 3b | **Body fetch** *(only if `can_store_full_text`)* | permitted articles | `cleaned_articles` | Per-article; skipped entirely for most sources |
| 4 | **Classify** | articles, `country_aliases`, `category_defs` | `article_countries`, `article_categories` | Falls back to source country; never blocks |
| 5 | **Embed + cluster** | articles | `embeddings`, `story_clusters` | Cluster of one on failure |
| 6 | **PUBLISH** | classified articles | `ingest_status = PUBLISHED` | **Site is live from this point** |
| 7 | **Enrich** *(optional)* | top-ranked clusters | `summaries` | Best-effort; quota-capped; never blocks |

**The publish barrier at stage 6 is the whole point of A3.** Stages 1–6 make zero external AI calls. Stage 7 can fail completely — quota exhausted, model deprecated, provider down — and the site is unaffected.

### Reliability, concretely

- **Per-source isolation.** Each source runs in its own `try/except` with its own DB transaction. `asyncio.gather(..., return_exceptions=True)` — never bare `gather`.
- **Exponential backoff with jitter:** 1s, 2s, 4s, three attempts, ±25% jitter so 200 sources do not retry in lockstep.
- **Circuit breaker.** `consecutive_failures >= 6` auto-sets `is_active = false` and raises an admin flag. A zero-article 200 response counts as a failure (A8).
- **Per-domain token bucket** — the existing `RateLimiter`, unchanged. It already works.
- **Conditional requests** — the existing `ConditionalRequestHeaders`, but persisted to the DB instead of in-memory, since the Actions runner is destroyed after each run. This makes ETags actually useful and cuts bandwidth substantially.
- **Storage guard.** Before stage 1: if `pg_database_size() > 85%` of quota, skip ingestion, run retention, alert.
- **LLM quota ledger.** `ingestion_runs.llm_calls_used` tracks the daily total; stage 7 stops when the cap is reached rather than discovering it via 429s.

### Retention (runs nightly, built in Phase 1)

| Age | Action |
|---|---|
| 7 days | Delete embeddings (clustering window is 72h) |
| 30 days | Delete `cleaned_articles.clean_text` where present |
| 90 days | Trim article to title/url/publisher/date/country; detach from cluster |
| 180 days | Delete `user_reading_history` rows |

---

## 6. Source / plugin architecture

Every ingestion method implements one interface. Adding a source is a database row plus, at most, a config entry — never a code change.

```python
class SourceAdapter(ABC):
    method: Literal["api", "rss", "scrape"]

    @abstractmethod
    async def preflight(self, source: Source, perms: SourcePermissions) -> PreflightResult:
        """Verify this source may be fetched right now.
        Checks robots.txt (fail-closed), rate-limit budget, API key presence,
        and permission review status. Returns ALLOW / DENY(reason) / DEFER."""

    @abstractmethod
    async def fetch(self, source: Source) -> list[RawItem]:
        """Return raw, source-shaped items. No normalization here."""

    @abstractmethod
    def normalize(self, item: RawItem, source: Source) -> NormalizedArticle:
        """Map source-shaped data into the common model (§7)."""

    def health(self, result: FetchResult) -> SourceHealth:
        """Default implementation; adapters may override."""
```

The registry resolves `source.ingestion_method` to an adapter class. `preflight` is not optional and cannot be skipped — it is called by the orchestrator, not by the adapter, so a buggy adapter cannot bypass compliance. This is the structural fix for A9.

**Source configuration lives in version-controlled YAML** and is synced into the DB by a migration-style loader, so source definitions are reviewable in pull requests:

```yaml
# data/sources/in.yaml
- name: The Hindu
  homepage_url: https://www.thehindu.com
  ingestion_method: rss
  endpoint: https://www.thehindu.com/news/national/feeder/default.rss
  country: IN
  language: en
  editorial_weight: 0.9
  rate_limit_rps: 0.5
  permissions:
    can_store_full_text: false      # not reviewed / not licensed
    can_store_image: false
    max_description_chars: 300
    terms_url: https://www.thehindu.com/termsofuse/
    review_notes: "RSS feed publicly offered. Metadata + link only."
```

**Target: 4–8 sources per enabled country, minimum 3.** Below three, one publisher's editorial slant becomes the country's news. The admin dashboard flags any enabled country with fewer than three healthy sources.

---

## 7. Article data model

The common internal model every adapter must produce:

```python
@dataclass
class NormalizedArticle:
    # Identity
    url: str                     # as published
    canonical_url: str           # utm/fbclid stripped, host lowercased
    url_hash: str                # sha256(canonical_url) — the dedup key (A4)

    # Content (permission-gated — see below)
    title: str
    description: str | None
    content: str | None          # None unless source.can_store_full_text
    image_url: str | None        # None unless source.can_store_image

    # Attribution — never optional
    source_id: int
    publisher_name: str
    publisher_url: str
    author_name: str | None
    published_at: datetime       # UTC, tz-aware
    requires_attribution: bool

    # Geography & taxonomy
    primary_country: str | None      # ISO2
    countries: list[CountryRelevance]
    region: str | None
    is_international: bool
    categories: list[CategoryAssignment]
    language: str                    # BCP-47

    # Provenance
    source_type: Literal["api", "rss", "scrape"]
    permissions: PermissionSet
    fetched_at: datetime
    updated_at: datetime

    # Derived
    simhash: int | None
    embedding: list[float] | None
```

**Three invariants enforced in the constructor, not by convention:**

1. `content` is `None` unless `permissions.can_store_full_text` is `True`. Not "should be" — the constructor raises. A bug in an adapter cannot produce an article carrying text it has no right to.
2. `description` is truncated to `permissions.max_description_chars` at construction.
3. `url`, `publisher_name`, `title`, and `published_at` are non-null. The existing `validate_attribution()` already enforces this correctly at insert time in `article_inserter.py` (it skips non-compliant articles) — this moves the same check up to the type level, so it holds for API and scrape adapters too, not just the RSS path.

The same `PermissionSet` travels with the article all the way to the API serializer (§11), so the guarantee holds end-to-end rather than only at ingest.

---

## 8. Country and category classification

The most important subsystem. Five tiers, each running only if the previous was inconclusive. **Tiers 0–3 are pure Python with zero API calls** and resolve the large majority of articles.

### Tier 0 — Source prior (free, ~70% accurate alone)

Every source has `country_id` and `is_international`. A Times of India article defaults to India at confidence 0.6. This is the fallback that guarantees every article gets *some* country, which is what makes the LLM optional.

### Tier 1 — Feed section prior

Per-source URL-path rules: `/world/`, `/international/`, `/global/` → international; `/india/`, `/uk/` → that country. Costs one regex. Configured per source in YAML, so a publisher's own section taxonomy does the work for you.

### Tier 2 — Weighted gazetteer over title + description

Match `country_aliases` against the text. Weights, not booleans:

| Alias type | Weight | Rationale |
|---|---|---|
| Demonym ("Brazilian", "Japanese") | 1.0 | Almost never ambiguous |
| Capital city ("Brasília", "Tokyo") | 0.9 | Strong, rarely collides |
| Government terms ("Bundestag", "Knesset", "Lok Sabha") | 1.0 | Unique by construction |
| Head of state / government (current) | 0.8 | High signal, needs maintenance |
| Country name ("Brazil", "Japan") | 0.7 | Ambiguous often enough to discount |
| Major city ("Mumbai", "Osaka") | 0.6 | |
| Currency ("yen", "rupee") | 0.4 | Weak alone |
| ISO code ("BRA") | 0.3 | Frequent false positives |

**Positional boost:** a match in the title scores 1.5×. Headlines are about their subject; body text mentions everything.

**The ambiguity guard.** Aliases flagged `is_ambiguous` score **zero** unless one of their `requires_context` terms also appears:

```
"Georgia"  → needs {tbilisi, caucasus, georgian, abkhazia, south ossetia}
"Jordan"   → needs {amman, jordanian, hashemite, king abdullah}
"Turkey"   → needs {ankara, istanbul, turkish, erdogan}
"Guinea"   → needs {conakry, guinean}  ... and Guinea-Bissau / Equatorial Guinea
             / Papua New Guinea are separate aliases matched longest-first
"China"    → needs {beijing, chinese, xi jinping, shanghai, prc}
```

Longest-match-first prevents "Papua New Guinea" from scoring Guinea. "Michael Jordan retires" scores nothing for Jordan, falls through to Tier 0, and is correctly filed under the source's country as a sports story.

### Tier 3 — Decision with confidence and margin

```
primary_country = argmax(scores)   IF   top_score >= 0.55
                                   AND  (top_score - runner_up) >= 0.20
else                               →    Tier 0 source country, confidence 0.4, flagged for review
```

The **margin** requirement is what stops a US–China trade story from being arbitrarily filed under whichever scored 0.01 higher. Both clear the threshold, neither wins the margin → the article becomes international with both countries as `secondary`.

`is_international` is true when **any** of:
- two or more countries score ≥ 0.5
- the section prior said international
- an international-organization alias matched (UN, NATO, EU, WTO, IMF, OPEC, G20, WHO, COP)
- the source itself is flagged `is_international`

This is why nothing is forced into exactly one country, as your brief requires.

### Tier 4 — LLM adjudication (optional, batched, capped)

Only articles that failed the Tier 3 margin test. **20 articles per prompt**, returning a JSON array. Hard cap of 30 calls/day from the quota ledger. If the cap is hit or the provider errors, Tier 3's fallback stands and the site is unaffected.

Every assignment stores `method` and `confidence`, which gives the admin panel a review queue ordered by lowest confidence — and lets you re-run the classifier over history whenever you improve the gazetteer.

### Category classification

Same shape, simpler. Weighted keyword lists per category, plus the source's own feed-section signal (a `/business/` feed is a strong prior). Below `MIN_CONFIDENCE_SCORE`, assign **"General"** as your brief specifies — note the current code assigns `"Uncategorized"`, which is a small inconsistency to fix. The existing 7-category list expands to your 13, plus General, in `category_defs`.

### Honest accuracy expectation

Tiers 0–3 should land around **88–93%** primary-country accuracy on English-language sources, degrading for multilingual and heavily entity-driven articles. That is good enough to ship, and the low-confidence review queue is how it improves. Anyone promising 99% from rules is not counting the failures.

---

## 9. Duplicate detection and story clustering

The existing deduplicator marks articles `DUPLICATE` and the API excludes them. Your brief explicitly rejects this: *"Do not simply delete duplicate articles."* And it is the wrong product decision anyway — five publishers covering one story is the most valuable signal you have, not noise.

**Two distinct concepts, currently conflated:**

- **A true duplicate** is the *same article*: identical `url_hash`, or the same publisher re-posting with a tracking parameter. These are dropped at ingest.
- **A story** is the *same event covered by different publishers*. These are clustered and shown together.

### The pipeline

**Stage 1 — Blocking (cheap, removes 99.9% of comparisons).** Never compare every article to every other. A candidate pair must satisfy all three:

- published within a **72-hour window**
- share at least one country assignment
- title SimHash Hamming distance ≤ 8, **or** `pg_trgm` title similarity > 0.3

**Stage 2 — Vector similarity.** Embed `title + ". " + description` with MiniLM (in the Actions runner — free, no quota). Query pgvector: `ORDER BY centroid <=> $1 LIMIT 20` against cluster centroids inside the window. This is an HNSW index scan, not a table scan.

**Stage 3 — Incremental single-link assignment.**

```
best = argmax(cosine(article, cluster.centroid))
if best.similarity >= 0.78:  join cluster; update centroid as running mean
else:                        create new cluster of one
```

Comparing to **centroids** rather than to every member is what makes this O(clusters in window) instead of O(articles²). At 50 articles/hour and a 72-hour window that is a few hundred comparisons, done in the database.

**Threshold honesty:** 0.78 is a starting point, not a derived constant. It needs tuning against a hand-labelled set of ~200 article pairs, and the admin panel should expose it as a setting. Too low merges unrelated stories, which is far more visible and damaging than too high leaving duplicates.

### Canonical article selection

The cluster's headline comes from `ORDER BY source.editorial_weight DESC, published_at ASC` — the most authoritative source, tie-broken by who reported first. Never a synthesized or AI-written headline: your brief is explicit that the app must not appear to have written the article, and a generated headline attributed to a cluster of real publishers is exactly that failure.

### What the user sees

```
┌────────────────────────────────────────────────┐
│  Magnitude 7.1 earthquake strikes northern     │
│  Japan                                          │
│  🌏 International · Japan · 3 sources           │
│                                                 │
│  ├ BBC News        2h ago    Read original →   │
│  ├ NHK World       2h ago    Read original →   │
│  └ The Guardian    1h ago    Read original →   │
└────────────────────────────────────────────────┘
```

Nothing is hidden, every publisher keeps its link, and the multi-source count becomes the ranking signal for the homepage.

---

## 10. Security model

### Secrets

Nothing but env-var **names** ever touches the database. `sources.api_key_ref` stores `"NEWSAPI_KEY"`, never the key. Secrets live in GitHub Actions secrets (ingestion), Render env vars (API), and Vercel env vars (frontend, public values only). Every API call requiring a secret happens server-side; the Next.js client bundle receives none. A CI check greps the build output for anything matching known key prefixes and fails the build on a hit.

### Malicious article content — the highest-probability attack

You are ingesting untrusted HTML from ~200 servers you do not control. Assume it is hostile.

- **Sanitize on ingest and again on render.** `bleach` with a strict allowlist server-side; never `dangerouslySetInnerHTML` on publisher content in React. Escape by default.
- **Validate every URL** against an `https?://` scheme allowlist. Reject `javascript:`, `data:`, `vbscript:`. This is the one that actually bites people — a `javascript:` URL rendered into an `<a href>` is stored XSS.
- **Never hotlink images.** `image_url` is validated as an image content-type, and a strict CSP `img-src` allowlist applies. Better: proxy through Next.js `<Image>` with `remotePatterns`, which also solves layout shift and bandwidth.
- **Every external link gets `rel="noopener noreferrer nofollow"` and `target="_blank"`.** `nofollow` also protects you from being an SEO amplifier for a compromised source.
- **SSRF guard on outbound fetches.** Refuse to fetch RFC1918 addresses, `localhost`, `169.254.169.254`, or anything resolving to a private IP. Without this, a malicious feed entry pointing at your cloud metadata endpoint is a credential leak.
- **Size caps.** `MAX_ARTICLE_LENGTH_CHARS` already exists; add a response-size cap and a decompression-bomb guard.

### API surface

- Read-only public API. No `POST`/`PUT`/`DELETE` outside `/admin`.
- Rate limiting per IP via `slowapi`, and per-endpoint caps on `limit` (max 100) so nobody scrapes your database by asking for `limit=1000000`.
- Pydantic validation on every query parameter; `country` must match `^[a-z]{2}$`, `category` must exist in `category_defs`.
- CORS locked to the Vercel domain. The current `FRONTEND_URL: str = "*"` default with `allow_credentials=True` is an invalid and permissive combination — fix it to an explicit origin list.
- Cache-Control headers on every public response so Vercel and the CDN do the work.

### Admin

Phase 4: HTTP Basic over TLS plus an IP allowlist, and a single admin account. Adequate for one operator. Phase 5: proper sessions, `argon2id` hashing, CSRF tokens on mutations, audit log of every source/permission change.

### Why user accounts are deferred

The moment you store passwords you own credential stuffing, reset-token security, session fixation, GDPR data-subject requests, and breach notification duties. **Follows, saved articles, and hidden sources all work perfectly in `localStorage`** and deliver identical value on a single device. Ship accounts when users ask for cross-device sync — the schema (§3.8) is already there, so it is an additive change, not a rewrite.

---

## 11. Legal and copyright

Your decision — *metadata and links by default, full text only where the site allows it* — is the right one, and it is now enforced structurally rather than by discipline.

### The default posture

| Field | Default | Basis |
|---|---|---|
| Headline | Stored & displayed | Short factual statement; standard aggregator practice |
| Publisher, author, date | Stored & displayed | Attribution — required, not optional |
| Description | Stored, **capped at 300 chars** | Publisher-supplied in the RSS feed for this purpose |
| Original URL | Stored & always displayed | The entire point |
| Image | **Not stored by default** | Separately licensed, often third-party (Getty/AP), highest risk per item |
| Full text | **Not stored by default** | Requires explicit per-source review |
| AI summary | Generated from permitted content only | Displayed *alongside* attribution, never in place of it |

### Three enforcement points

1. **Ingest.** `SourceAdapter.preflight()` denies unless permissions allow. Body fetching does not run for sources without `can_store_full_text`.
2. **Model.** `NormalizedArticle.__post_init__` raises if `content` is set without permission. A bug cannot produce an illegal record.
3. **Serialization.** The API serializer re-checks permissions before emitting. Even a corrupted row cannot be published.

Defence in depth, because "we'll be careful" is not a legal strategy.

### Attribution is structural, not cosmetic

Publisher name is required on every card. "Read original article →" is a primary, high-contrast affordance — never a small grey link at the bottom. Article pages carry `<link rel="canonical">` pointing at the **publisher's URL**, not yours. That last point matters twice over: it is the honest signal to search engines about who wrote the piece, and it is your defence against the accusation that you are competing for the publisher's own search traffic.

### Things I want to say plainly

- **robots.txt permission ≠ copyright permission.** They answer different questions (A9). The current code conflates them.
- **"Publicly accessible" is not "licensed."** Your brief already says this; the code does not yet implement it.
- **AI summaries are derivative works.** A summary generated from an article is derived from that article. Keeping them short, factual, and attributed — and linking prominently to the original — is the defensible posture. Generating a long summary that substitutes for reading the original is not, regardless of how the model produced it.
- **Wire services (AP, Reuters, AFP) require paid licences.** There is no free-tier path to them. This design excludes them and says so rather than routing around it through third-party scrapers (A8).
- **A takedown path is required.** A `/contact` page, a monitored address, and a documented process to disable a source and purge its articles within 24 hours. Publishers who want out should have an easy, obvious way to ask — most disputes end there. Add a `sources.takedown_requested_at` column so the state is recorded.
- **I am not a lawyer and this is not legal advice.** This is a defensible engineering posture based on how established aggregators operate. If ClarityFeed gets real traffic or you monetize it, get an actual opinion from a lawyer in your jurisdiction — the analysis differs materially between India, the EU (which has an Article 15 press-publishers' right covering exactly this), and the US.

---

## 12. API design

Base `/api/v1`. Read-only, cursor-paginated, cacheable.

```
GET /api/v1/news                    ?country&category&language&publisher&from&to&cursor&limit
GET /api/v1/news/:id
GET /api/v1/international           top international stories
GET /api/v1/countries               ?enabled&region&q     — the country selector
GET /api/v1/countries/:iso2
GET /api/v1/countries/:iso2/news    ?category&cursor&limit
GET /api/v1/regions
GET /api/v1/categories
GET /api/v1/stories/:slug           a cluster with all its sources
GET /api/v1/search                  ?q&country&category&publisher&language&from&to&cursor
GET /api/v1/sources                 ?country&enabled  (public metadata only — no keys, no endpoints)
GET /api/v1/feed                    ?countries=in,us,jp&categories=business  — personalized, stateless
```

**Cursor pagination, not offset.** `OFFSET 10000` makes Postgres scan and discard 10,000 rows. A keyset cursor — base64 of `(published_at, id)` — is an index seek at any depth, and it does not skip or repeat items when new articles arrive mid-scroll, which offset pagination does constantly on a live news feed.

**`/feed` is stateless by design.** Countries come in as a query parameter, so it works for anonymous `localStorage` users in Phase 3 and for logged-in users in Phase 5 with no API change.

Response shape:

```json
{
  "data": [{
    "id": 84021,
    "title": "…",
    "description": "…",
    "content": null,
    "url": "https://publisher.example/article",
    "publisher": { "name": "The Hindu", "url": "…", "logoUrl": null },
    "author": "…",
    "publishedAt": "2026-08-09T06:14:00Z",
    "primaryCountry": { "iso2": "in", "name": "India", "flag": "🇮🇳" },
    "countries": [{ "iso2": "in", "relevance": "primary" }],
    "isInternational": false,
    "categories": [{ "slug": "business", "isPrimary": true }],
    "language": "en",
    "imageUrl": null,
    "cluster": { "slug": "…", "sourceCount": 4 },
    "summary": { "tldr": "…", "generatedBy": "gemini-2.5-flash-lite" },
    "attribution": { "required": true, "readOriginalUrl": "https://publisher.example/article" }
  }],
  "pagination": { "nextCursor": "eyJ0IjoiMjAyNi0wOC0wOVQwNjoxNDowMFoiLCJpIjo4NDAyMX0", "hasMore": true }
}
```

`content: null` and a populated `attribution` block are the visible result of the permission model. Every response carries `Cache-Control: public, s-maxage=300, stale-while-revalidate=600`.

**Errors:** RFC 7807 problem+json. **Versioning:** `/v1` in the path from day one — retrofitting it later is painful.

---

## 13. Frontend structure

### Routes

| Route | Rendering | Purpose |
|---|---|---|
| `/` | ISR 300s | Global homepage — top international + country rails |
| `/international` | ISR 300s | International only |
| `/[country]` | ISR 300s, `generateStaticParams` for enabled countries | Country front page |
| `/[country]/[category]` | ISR 600s | Country + category |
| `/story/[slug]` | ISR 300s | Cluster: one event, every source |
| `/article/[id]` | ISR 3600s | Reading experience |
| `/search` | Client + server action | Search with filters |
| `/my-feed` | Client only | `localStorage` follows |
| `/countries` | Static | Full browsable country index |
| `/admin/*` | Client, auth-gated | §14 |

Plus `sitemap.xml` (segmented by country), `robots.txt`, and RSS output per country — a news aggregator that does not itself publish feeds is missing an obvious channel.

### The design brief, concretely

You said: no generic SaaS dashboard, no gradients, no glassmorphism, no giant rounded cards, no AI slop. Translated into decisions:

- **Type is the design.** One serif for headlines (Source Serif 4 / Newsreader) at genuinely large sizes with tight leading; one grotesque for metadata and UI (Inter). A real modular scale — 12/14/16/20/28/40/56 — not four sizes of medium-grey text.
- **Rules, not cards.** Editorial hierarchy comes from 1px hairlines, column structure, and whitespace. Border-radius 0 to 2px. No box-shadows. If a section needs a card to feel separate, the typography is not doing its job.
- **Restrained colour.** Near-black on off-white (`#111` on `#FAF9F7`). One accent for links and live indicators. Country identity comes from flags and names, not from tinting the page.
- **Density is a feature.** Someone checking the news at 7am wants many headlines visible at once, not four hero cards. Desktop homepage should show 40+ headlines above two scrolls.
- **Motion is almost absent.** 150ms opacity on hover. No entrance animations, no parallax, no skeleton shimmer — use dimmed real layout instead.
- **Attribution is typographic furniture.** Publisher name in small caps with letterspacing, next to a relative timestamp, on every single item. It should read as part of the design, not as a legal disclaimer.

### Homepage structure

```
┌──────────────────────────────────────────────────────────────┐
│ CLARITYFEED          Search    🌍 Countries ▾    My Feed  ☰  │
│──────────────────────────────────────────────────────────────│
│ 🇮🇳 India  🇺🇸 US  🇬🇧 UK  🇯🇵 Japan  🇩🇪 Germany  + Add     │  ← pinned rail, sticky
├──────────────────────────────────────────────────────────────┤
│ INTERNATIONAL                                    Friday 9 Aug │
│ ──────────────────────────────────────────────────────────── │
│ ┌────────────────────────────┬─────────────────────────────┐ │
│ │  Lead story, serif 40px     │ Second story                │ │
│ │  REUTERS · 2h · 6 sources   │ BBC · 3h · 4 sources        │ │
│ │  Two-line standfirst.       │─────────────────────────────│ │
│ │  Read original →            │ Third story                 │ │
│ │                             │ AL JAZEERA · 4h             │ │
│ └────────────────────────────┴─────────────────────────────┘ │
├──────────────────────────────────────────────────────────────┤
│ 🇮🇳 INDIA                                        View all →   │
│ ──────────────────────────────────────────────────────────── │
│ Headline one          Headline two         Headline three     │
│ THE HINDU · 1h        NDTV · 2h            MINT · 2h          │
└──────────────────────────────────────────────────────────────┘
```

### Mobile

Your brief is right that country switching must not be buried. The pinned country rail is horizontally scrollable and **sticky at the top on every page**, so switching country is always one tap. The "+ Add" chip opens a full-screen searchable country sheet. No hamburger required to change country — ever.

### Performance

Server components by default; client components only for the country sheet, search box, and follow buttons. No client-side data fetching on first paint. Target: LCP < 1.5s on 4G, JS bundle < 100 KB gzipped, CLS 0 (every image slot has reserved dimensions).

---

## 14. Admin panel

`/admin`, auth-gated. This is an operational tool, so it should be dense and ugly-in-a-good-way.

**Dashboard** — the numbers that tell you whether the system is healthy:

```
Last run  06:00 UTC  ✅ 4m 12s        Articles today       1,247
Sources   187 active / 9 failing      New clusters today      88
Storage   142 MB / 500 MB  (28%)      Duplicate rate       31.4%
LLM quota 41 / 200 today              Low-confidence queue    62 ⚠
```

Plus: articles by country (bar), articles by category, ingestion volume over 14 days, and — most importantly — **a failing-sources table sorted by consecutive failures**, which is how you catch A8 before it becomes a coverage hole.

**Sources** — table (name, country, language, method, status, last success, articles last run, consecutive failures, permission-reviewed?) with row actions Enable / Disable / **Test** / Edit / Delete. "Test" runs preflight + a single live fetch and shows the raw parsed result without writing to the database. That one button is worth more than every chart on the dashboard.

**Permissions** — a dedicated editor per source for the `source_permissions` row, with `terms_url`, `robots_url`, review notes, and reviewer/timestamp stamped automatically on save. Sources with `reviewed_at IS NULL` are pinned to the top.

**Countries** — enable/disable, edit slug and region, and **manage the alias gazetteer**: add aliases, set weights, mark ambiguous, define required-context terms. This is where classification accuracy actually gets improved, so it should be a first-class screen, not a JSON textarea.

**Categories** — CRUD on `category_defs`, reorder, enable/disable, edit keyword rules. Your brief requires categories be manageable without code changes; this is that.

**Review queue** — low-confidence country and category assignments, ordered ascending by confidence, with one-click correct-and-relabel. Corrections are stored with `method = manual` and become the labelled set for tuning thresholds.

**Runs** — `ingestion_runs` history with per-stage counts and error summaries, plus a **Run now** button for breaking news.

---

## 15. Development roadmap

Each phase is independently shippable and leaves the system in a working state.

### Phase 0 — Stop the bleeding (½ day)
Fix the things that are actively broken before building on them.
- Alembic initialized; baseline migration stamped against the current schema
- `url_hash` added, broken unique index dropped (A4)
- Groq model names updated; provider abstraction stubbed (A1/A2)
- Dead Reuters and RSSHub/AP sources removed (A8)
- robots.txt fallback inverted to fail-closed (A9)
- CORS `*` + `allow_credentials` fixed
**Ships:** nothing visible. Everything after this is safe to build on.

### Phase 1 — Geography and permissions (3–4 days)
- Migrations: `regions`, `countries`, `country_aliases`, `category_defs`, `article_countries`, `article_categories`, `source_permissions`
- Seed ~50 countries, ~15 regions, full gazetteers for the first 12 countries
- Rule-based country + category classifier (Tiers 0–3)
- Permission gate wired into ingest and serialization
- **Retention job — built now, not deferred** (A6)
- Storage guard
**Ships:** a correctly classified, legally-postured database. Still no UI.

### Phase 2 — Pipeline relocation (2–3 days)
- Ingestion moves into GitHub Actions; cadence to hourly (A7)
- Local MiniLM embeddings; HuggingFace dependency deleted
- pgvector extension, `halfvec(384)`, HNSW index; `vector_json` backfilled and dropped (A5)
- `ingest_status` / `enrichment_status` split; publish barrier (A3)
- `source_health`, `ingestion_runs`, circuit breaker
- **Decision point: migrate to Supabase or stay on Neon** (A7)
**Ships:** a pipeline that runs inside its free-tier budget and cannot be taken down by an AI vendor.

### Phase 3 — Clustering + public API (3–4 days)
- SimHash, blocking, incremental single-link clusterer
- `story_clusters`, `story_cluster_articles`
- Full `/api/v1/*` surface with cursor pagination
- Postgres full-text search with filters
**Ships:** a complete, queryable news API.

### Phase 4 — Frontend + admin (5–7 days)
- Next.js: homepage, country pages, story pages, article pages, search, country selector, `/my-feed` on `localStorage`
- Sitemaps, per-country RSS, canonical tags to publishers
- Admin: dashboard, sources, permissions, countries/gazetteer, categories, review queue, runs
**Ships:** the actual product.

### Phase 5 — Enrichment and accounts (3–5 days)
- Gemini/Groq provider abstraction, batched, quota-ledgered, ranker-driven
- Tier 4 LLM adjudication for low-confidence classifications
- Optional user accounts; `localStorage` follows migrate on first login

### Phase 6 — Expansion (ongoing)
Source expansion to 40+ countries, non-English gazetteers, translation, event timelines, breaking-news detection, news map, personalized ranking, source-bias comparison.

**Estimate to a live, useful product: Phases 0–4, roughly 14–19 working days.**

---

## 16. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Free LLM quota cut or model deprecated | **Certain** — happened twice already | Low, by design | Publish barrier (A3); two providers; rules-first classification |
| 2 | Neon compute-hours exhausted mid-month | **High** if unchanged | Site stops updating | Hourly cadence + Supabase migration (A7) |
| 3 | Database hits 0.5 GB | High without action | Writes fail | Retention from Phase 1 + storage guard (A6) |
| 4 | Publisher takedown request | Moderate | Reputational + legal | Metadata-only default, permission table, 24h takedown process (§11) |
| 5 | Wrong country on the homepage | Moderate | **Trust — the worst failure** | Ambiguity guards, margin thresholds, review queue (§8, A10) |
| 6 | Feeds die silently | **Certain** over time | Coverage holes | Zero-article-as-failure, circuit breaker, admin alerts (A8) |
| 7 | Stored XSS from a malicious feed | Moderate | Severe | Sanitize on ingest + render, URL scheme allowlist, CSP (§10) |
| 8 | SSRF via crafted feed URL | Low | Severe | Private-IP resolution blocklist (§10) |
| 9 | Clustering merges unrelated stories | Moderate | Very visible | Conservative 0.78 threshold, blocking by country + time, tunable |
| 10 | Render cold start hurts users | Low, by design | Minor | ISR means users hit Vercel's edge, not Render |
| 11 | GitHub disables the cron after 60 days idle | **High** | Ingestion stops | Weekly auto-commit step in the workflow (§1) |
| 12 | SEO: seen as a scraper / thin content | Moderate | No organic traffic | Canonical to publisher, `nofollow` externals, cluster pages as the genuinely original contribution |
| 13 | Search degrades past ~500k articles | Low near-term | Slow search | Postgres FTS + GIN now; Meilisearch when it actually hurts |
| 14 | Non-English classification is weak | **Certain** initially | Poor non-English coverage | Ship English-first, honestly; expand gazetteers per language in Phase 6 |
| 15 | Single operator, no on-call | Certain | Slow recovery | Everything degrades rather than fails; admin dashboard as the daily check |

---

# PART C — Alembic migration plan

Your instruction: preserve everything, do not drop or recreate, keep the pipeline working throughout. That constrains the ordering — every migration must leave the existing code runnable.

| Rev | Migration | Backward compatible? |
|---|---|---|
| 001 | `stamp_baseline` — capture the current 6-table schema as the starting point. No DDL. | Yes |
| 002 | `add_url_hash` — add nullable `url_hash`; backfill in Python; add unique index; **then** drop the `url` unique constraint. Three steps, one revision. | Yes |
| 003 | `add_geography` — `regions`, `countries`, `country_aliases`. Pure additive. | Yes |
| 004 | `add_taxonomy` — `category_defs`; rename `categories` → `article_classifications`; add `article_categories`. Rename is the only breaking step; the categorizer import is updated in the same commit. | Requires code deploy |
| 005 | `add_article_countries` — table, denormalized `published_at`, sync trigger, composite index. Backfill from `sources.country`. | Yes |
| 006 | `add_source_permissions` — 1:1 table; insert a restrictive-default row for every existing source. | Yes |
| 007 | `extend_sources` — new columns, all nullable or defaulted; backfill `country_id` from the existing `country` string. | Yes |
| 008 | `extend_articles` — `description`, `image_url`, `author_name`, `language`, `simhash`, `primary_country_id`, `is_international`, `search_vector` + GIN. | Yes |
| 009 | `split_status` — add `ingest_status` / `enrichment_status`; backfill from `status` (`PROCESSED` → `PUBLISHED`); keep `status` as a synced shadow column for one release, then drop in 013. | Yes |
| 010 | `pgvector` — `CREATE EXTENSION vector`; add `embeddings.vector halfvec(384)`; backfill from `vector_json`; HNSW index; drop `vector_json` **in a later revision** after verification. | Yes |
| 011 | `add_clusters` — `story_clusters`, `story_cluster_articles`, `raw_articles.cluster_id`. | Yes |
| 012 | `add_users` — all seven user tables, created empty. | Yes |
| 013 | `add_ops` — `ingestion_runs`, `source_health`; drop the shadow `status` and `vector_json`. | Requires 009/010 verified |

Rules I will follow: never `DROP` in the same revision that adds the replacement; backfills run in batches with progress logging, not one giant `UPDATE`; every revision has a tested `downgrade()`; migrations run in CI against a snapshot of the schema before they run against production.

---

# PART D — What I need from you before implementing

1. **Approve or amend this design.** Particularly §8 (classification) and §11 (legal posture) — those are the two that are expensive to change later.

2. **Neon or Supabase?** (A7) This is the one decision that blocks Phase 2 and it is genuinely load-bearing. My recommendation is Supabase, because Neon's 100 compute-hours/month is a hard wall that this workload hits in about nine days. Migration cost is a connection string.

3. **Gemini key?** (A2) Get one from Google AI Studio — free, no card. Keep the Groq key as fallback. Note the free-tier training term.

4. **Which countries first?** I suggest starting with 12 well-sourced ones — India, US, UK, Canada, Australia, Germany, France, Japan, Brazil, South Africa, Singapore, UAE — rather than 50 thin ones. Better to have four good sources for twelve countries than one dead feed for fifty. Tell me if that list is wrong for your audience.

5. **Public or private repo?** README says open-source. Public gives unlimited free Actions minutes, which this architecture depends on. Private caps you at 2,000 minutes/month, which hourly ingestion would exceed.

6. **Confirm the phase order**, or tell me to jump ahead. My strong recommendation is Phase 0 first — six small fixes, half a day, and everything after it is built on solid ground instead of on a schema that throws index errors on Japanese URLs.

