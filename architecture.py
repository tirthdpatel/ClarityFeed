"""
ClarityFeed — Architecture Decision Record (ADR)

This file is documentation only — it contains no runnable code.
All architectural decisions, interface contracts, and failure modes
are documented here as the single source of truth.

==========================================================================
1. LOGICAL PIPELINE
==========================================================================

RSS Feeds → Feed Collector → Article Fetcher → Content Cleaner
    → Deduplicator → Embedding Generator → Summarizer → Categorizer
    → Database Storage → REST API → Frontend

Each stage has a single responsibility:
  - Feed Collector    : fetch and parse RSS/Atom feeds, insert raw articles
  - Article Fetcher   : download full article HTML (Phase 2)
  - Content Cleaner   : extract clean text from raw HTML (Phase 2)
  - Deduplicator      : compare embeddings to find similar articles (Phase 2)
  - Embedding Generator: generate semantic vectors via HF API (Phase 2)
  - Summarizer        : produce AI summaries via Groq API (Phase 2)
  - Categorizer       : assign categories via Groq API (Phase 2)
  - Database          : persistent storage (Neon PostgreSQL)
  - REST API          : serve data to the frontend (FastAPI on Render)
  - Frontend          : user interface (React + Vite on Vercel)

Input/output contracts are defined as dataclasses in
``backend/database/models.py``.

==========================================================================
2. PHYSICAL HOSTING MAP
==========================================================================

+--------------------+---------------------+------------------------------+
| Pipeline Stage     | Hosted On           | Trigger                      |
+--------------------+---------------------+------------------------------+
| RSS Feed Collector | Render (FastAPI)    | GitHub Actions cron (15 min) |
| Article Fetcher    | Render (background) | Triggered by collector       |
| Content Cleaner    | Render (in-process) | Triggered by fetcher         |
| Deduplicator       | Render + HF API     | Triggered by cleaner         |
| Summarizer         | Render + Groq API   | Triggered by deduplicator    |
| Categorizer        | Render + Groq API   | Triggered by summarizer      |
| Database           | Neon PostgreSQL     | Always-on serverless         |
| REST API           | Render (FastAPI)    | Incoming HTTP requests       |
| Frontend           | Vercel (React+Vite) | HTTP requests from users     |
+--------------------+---------------------+------------------------------+

WHY EACH SERVICE:
  - Render.com    : free tier with no credit card, auto-deploy from GitHub,
                    native Python support, no Docker needed.
  - Neon.tech     : free serverless PostgreSQL, auto-suspend/resume, no
                    credit card, standard pg protocol.
  - Groq          : free LLM API (Llama 3.1, Mixtral), no credit card,
                    14,400 requests/day.
  - HuggingFace   : free Inference API for embeddings, no credit card.
  - Vercel        : free frontend hosting, auto-deploy, excellent Vite
                    support.
  - GitHub Actions : free cron scheduling for public repos.

==========================================================================
3. COLD-START BEHAVIOUR
==========================================================================

Neon auto-resume:
    Neon suspends the PostgreSQL compute after 5 minutes of inactivity.
    On the next connection, it resumes automatically (typically 2–5
    seconds). ``pool_pre_ping=True`` and ``init_db_with_retry()`` handle
    this transparently.

Render cold start:
    Render's free tier sleeps after 15 minutes of inactivity. The first
    HTTP request wakes the service (30–60 seconds). The GitHub Actions
    workflow handles this with a 3-attempt retry loop with 30-second
    delays.

Combined cold start:
    In the worst case, both Render and Neon are sleeping. The GitHub
    Actions trigger wakes Render (30–60s), which then connects to Neon
    (2–5s with ``init_db_with_retry``). Total cold-start time: ~35–65s.
    The GitHub Actions retry loop accommodates this.

==========================================================================
4. GITHUB ACTIONS → RENDER TRIGGER ARCHITECTURE
==========================================================================

WHY GITHUB ACTIONS REPLACES APSCHEDULER:
    APScheduler requires an always-on process. Render's free tier sleeps
    after 15 minutes. Running APScheduler on Render would mean:
      1. The scheduler sleeps and misses its intervals
      2. On wake, it may fire multiple missed jobs simultaneously
      3. Resource waste keeping the process alive just for scheduling

    GitHub Actions runs externally and independently:
      1. GitHub fires the cron at */15 * * * *
      2. The workflow sends an HTTP POST to Render
      3. Render wakes (if sleeping) and runs the collection
      4. Render goes back to sleep after inactivity

    This is more reliable, more debuggable (GitHub Actions logs), and
    costs nothing on public repositories.

INTERNAL_SECRET PROTECTION:
    The ``POST /internal/collect`` endpoint requires a header:
        ``X-Internal-Secret: <secret>``
    The secret is set as a GitHub Actions secret and a Render environment
    variable. The endpoint uses ``secrets.compare_digest()`` for constant-
    time comparison to prevent timing attacks.

==========================================================================
5. INTERFACE CONTRACTS (see backend/database/models.py)
==========================================================================

- PipelineStatus: PENDING, PROCESSED, FAILED, DUPLICATE
- SourceRecord: id, name, url, feed_url, language, country, category, is_active
- RawArticleRecord: id, source_id, url, title, published_at, raw_html, status
- CleanedArticleRecord: id, raw_article_id, clean_text, word_count, language
- EmbeddingRecord: id, raw_article_id, vector_json, model_name
- SummaryRecord: id, raw_article_id, summary_text, model_name, tokens
- CategoryRecord: id, raw_article_id, primary_category, confidence

==========================================================================
6. FAILURE MODE MAP
==========================================================================

+--------------------+----------------------------+-----------------------+
| Stage              | Failure Mode               | Recovery Strategy     |
+--------------------+----------------------------+-----------------------+
| GitHub Actions     | Render unreachable         | 3x retry, 30s delay   |
| RSS Collection     | Single feed HTTP error     | Log, skip, continue   |
|                    | Malformed feed XML         | Log, return empty     |
|                    | robots.txt fetch failure   | Permissive fallback   |
| Article Fetcher    | Article page timeout       | Mark FAILED status    |
|                    | Content extraction failure | Mark FAILED status    |
| Deduplicator       | HF API 503 (model loading) | Retry with backoff    |
|                    | HF rate limit exceeded     | Local fallback (opt)  |
| Summarizer         | Groq API rate limit        | Exponential backoff   |
|                    | Groq API error             | Fallback model        |
| Categorizer        | Groq API error             | Fallback model        |
| Database           | Neon connection timeout    | pool_pre_ping retry   |
|                    | Neon auto-resume delay     | connect_timeout=10    |
|                    | Duplicate article insert   | ON CONFLICT skip      |
| API                | Invalid INTERNAL_SECRET    | 403 Forbidden         |
|                    | Render cold start          | GitHub Actions retry  |
+--------------------+----------------------------+-----------------------+
"""
