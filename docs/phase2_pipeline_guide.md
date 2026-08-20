# Phase 2 Pipeline Operational Guide

## Full Pipeline Flow Diagram

```
                    ┌─────────────────────────────────────────────────────────────┐
                    │                     POST /internal/collect                   │
                    │                    (GitHub Actions Cron)                     │
                    └───────────────────────────┬─────────────────────────────────┘
                                                │
                                                ▼
┌───────────────────────────────────────────────────────────────────────────────────────────────┐
│  Stage 1: RSS Collection (Phase 1)                                                             │
│  • Fetches active sources, parses feeds                                                        │
│  • Inserts new articles into raw_articles (status=PENDING)                                     │
│  • Reads: sources | Writes: raw_articles                                                       │
└───────────────────────────────────┬───────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────────────────────────────┐
│  Stage 2: Article Fetching + Extraction                                                        │
│  • Fetches full HTML for PENDING articles                                                      │
│  • Extracts body: newspaper3k → readability → BeautifulSoup                                    │
│  • Inserts into cleaned_articles; clears raw_html (storage conservation)                       │
│  • Reads: raw_articles | Writes: cleaned_articles                                              │
└───────────────────────────────────┬───────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────────────────────────────┐
│  Stage 3: Content Cleaning                                                                     │
│  • 7 transforms: HTML strip, URL removal, UTM strip, whitespace, dedup sentences, Unicode     │
│  • Updates cleaned_articles in place                                                           │
│  • Reads: cleaned_articles | Writes: cleaned_articles                                          │
└───────────────────────────────────┬───────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────────────────────────────┐
│  Stage 4: Deduplication                                                                        │
│  • HuggingFace API: embed texts → 384-dim vectors                                              │
│  • Stores in embeddings; cosine similarity vs last N articles                                  │
│  • Marks duplicates as DUPLICATE                                                               │
│  • Reads: cleaned_articles | Writes: embeddings, raw_articles.status                           │
└───────────────────────────────────┬───────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────────────────────────────┐
│  Stage 5: Summarization                                                                        │
│  • Groq API: structured JSON (tldr + bullets)                                                  │
│  • Stores in summaries                                                                         │
│  • Reads: cleaned_articles | Writes: summaries                                                 │
└───────────────────────────────────┬───────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────────────────────────────┐
│  Stage 6: Categorization                                                                       │
│  • Groq API: zero-shot classification → 7 labels                                               │
│  • Stores in categories; marks raw_articles.status = PROCESSED                                 │
│  • Reads: raw_articles, summaries | Writes: categories, raw_articles.status                    │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Status Transition Table

| Status   | Set By                | Meaning                                           |
|----------|------------------------|---------------------------------------------------|
| PENDING  | RSS Collector (Phase 1)| Awaiting processing                               |
| FAILED   | Any stage              | Permanent failure; excluded from API              |
| DUPLICATE| Deduplicator (Phase 2) | Semantically duplicate; excluded from API         |
| PROCESSED| Categorizer (Phase 2)  | Fully processed; ready for API serving            |

## API Key Usage Summary

| Stage       | API                 | Est. Calls per Cycle | Daily Budget (96 cycles) | Free Tier Limit   |
|-------------|---------------------|----------------------|--------------------------|-------------------|
| Dedup       | HuggingFace Inference| ~1 batch per cycle   | ~96 batches              | 1000 req/day (varies) |
| Summarize   | Groq Llama/Mixtral   | ~30 articles         | ~2,880                   | 14,400/day        |
| Categorize  | Groq Llama/Mixtral   | ~30 articles         | ~2,880                   | 14,400/day        |

**Total Groq per day:** ~5,760 (summarize + categorize) — well within 14,400/day free limit.

## Debugging Guide

### Render Log Messages

- `Collection: {result}` — RSS collection summary
- `Fetch: FetchBatchResult(...)` — Article fetch/extract counts
- `Clean: CleanBatchResult(...)` — Cleaning result
- `Dedup: DeduplicationResult(...)` — Dedup result
- `Summarize: SummarizeBatchResult(...)` — Summarization result
- `Categorize: CategorizeBatchResult(...)` — Categorization result
- `Pipeline orchestration failed: ...` — Full pipeline exception

### Neon SQL Editor — Inspect Article Status

```sql
-- Count by status
SELECT status, COUNT(*) FROM raw_articles GROUP BY status;

-- Find FAILED articles
SELECT id, url, title, status FROM raw_articles WHERE status = 'failed';

-- Check summaries for an article
SELECT s.raw_article_id, s.tldr, s.bullet_points
FROM summaries s
JOIN raw_articles r ON r.id = s.raw_article_id
WHERE r.status = 'processed'
LIMIT 10;
```

### Manual Trigger via GitHub Actions

1. Open your repository on GitHub
2. Go to **Actions** → **collect** (or your workflow name)
3. Click **Run workflow** → **Run workflow** (workflow_dispatch)
4. The workflow triggers the `POST /internal/collect` endpoint with the secret header

## Storage Growth Estimate

- **Articles per cycle:** ~50
- **Cycles per day:** 96 (every 15 min)
- **Raw HTML:** Cleared after extraction (`DELETE_RAW_HTML_AFTER_CLEANING=True`)
- **Per article:** ~2–5 KB (cleaned text + embedding + summary + category)
- **Daily growth:** ~50 × 96 × 4 KB ≈ **19 MB/day**
- **500 MB limit:** Reached in approximately **26 days**

**Recommendation:** Phase 3 should add a 30-day article cleanup job to archive or delete old processed articles and stay under the Neon 0.5 GB free limit.

## Schema Migration for Existing Phase 1 Deployments

If you deployed Phase 1 before Phase 2, run these in the Neon SQL editor to add new columns:

```sql
ALTER TABLE summaries ADD COLUMN IF NOT EXISTS tldr TEXT;
ALTER TABLE summaries ADD COLUMN IF NOT EXISTS bullet_points TEXT;
```

Not all PostgreSQL versions support `IF NOT EXISTS` for columns. If you see an error, use:

```sql
ALTER TABLE summaries ADD COLUMN tldr TEXT;
ALTER TABLE summaries ADD COLUMN bullet_points TEXT;
```

(Use `IF NOT EXISTS` only if your Neon PostgreSQL version supports it.)
