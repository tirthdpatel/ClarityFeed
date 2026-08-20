# Technology Decisions — ClarityFeed

## Why Groq Instead of Local Ollama

Groq provides completely free API access to open-weight models (Llama 3.1 8B, Mixtral 8x7B) with no credit card required. Running Ollama locally would require a machine with a GPU or sufficient CPU/RAM, which contradicts the zero-cost, zero-device constraint. Groq's free tier grants 14,400 requests/day — far more than needed for 15-minute collection cycles processing 20–50 articles. Groq's inference speed (~500 tokens/second) is also significantly faster than CPU-bound local inference.

## Why Neon Instead of Local PostgreSQL

Neon provides a permanently free serverless PostgreSQL database (0.5 GB storage) that auto-suspends after 5 minutes of inactivity and auto-resumes on connection. No machine is needed, no credit card required. A local PostgreSQL would require a local machine or VPS, violating the zero-device constraint. Neon speaks standard PostgreSQL protocol, so switching to any other PostgreSQL provider requires only changing the connection string.

## Why GitHub Actions Instead of APScheduler

APScheduler requires an always-on process to fire scheduled jobs. Render's free tier sleeps after 15 minutes of inactivity. Running APScheduler on a sleeping service means missed schedules and unreliable triggering. GitHub Actions provides free cron scheduling for public repositories (2,000 minutes/month), runs independently of the Render service, and wakes the service via HTTP on each trigger. This is more reliable, more observable (built-in logs and manual re-run), and costs nothing.

## Why Render Instead of Railway or Fly.io

Render's free tier does not require a credit card. Railway requires a credit card for its trial tier. Fly.io requires a credit card on signup. Render provides 750 hours/month on the free tier, auto-deploys from GitHub, has native Python support (no Docker required), and provides environment variable management through its dashboard.

## Why Vercel Instead of Netlify

Both Vercel and Netlify offer free frontend hosting, but Vercel has superior Vite project detection and build configuration. Vite's `dist/` output is automatically detected. Vercel also provides better build performance and edge caching. No credit card required.

## Why HuggingFace Inference API for Embeddings Instead of a Local Model

The `sentence-transformers/all-MiniLM-L6-v2` model is available via HuggingFace's free Inference API. Loading `sentence-transformers` with PyTorch in-process would require ~500 MB of RAM for model weights alone, which would cause Render's free tier (512 MB) to OOM kill the process. The HTTP API call to HuggingFace uses negligible memory. An optional local fallback is implemented but disabled by default (`USE_LOCAL_EMBEDDING_FALLBACK=False`).

## Why `sentence-transformers` Is Excluded from `requirements.txt`

The `sentence-transformers` package depends on PyTorch (`torch`), which is ~2 GB installed. Including it would:
1. Cause Render builds to time out (free tier has a 15-minute build limit)
2. Cause OOM kills when the model is loaded (~500 MB RAM vs 512 MB limit)
3. Vastly increase cold-start time

All embedding generation is handled via the HuggingFace Inference API over HTTP, which requires no special packages beyond `httpx`.
