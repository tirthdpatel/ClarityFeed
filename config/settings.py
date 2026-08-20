"""
ClarityFeed configuration module.

All configuration values are loaded from environment variables with
python-dotenv fallback for local development. In production (Render),
environment variables are set in the Render dashboard.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    For local development, create a .env file (see .env.example).
    For Render deployment, set environment variables in the dashboard.
    """

    # ---- Database (Neon PostgreSQL) ----
    DATABASE_URL: str = "sqlite:///./test.db"
    DB_POOL_PRE_PING: bool = True
    DB_POOL_RECYCLE: int = 300
    DB_CONNECT_TIMEOUT: int = 10

    # ---- RSS Collection ----
    RSS_FETCH_INTERVAL_MINUTES: int = 15
    MAX_CONCURRENT_FETCHES: int = 5
    REQUEST_DELAY_SECONDS: float = 1.0

    # ---- LLM provider selection ----
    # "groq" or "gemini". The provider abstraction lives in backend/llm/.
    # Swapping providers is a config change, not a code change — this exists
    # because Groq has now deprecated models out from under us twice.
    LLM_PROVIDER: str = "groq"

    # ---- Groq LLM API (free, no credit card) ----
    #
    # MODEL NAMES — verified against console.groq.com/docs/deprecations 2026-08-19.
    #   llama-3.1-8b-instant   DEPRECATED 2026-08-16 -> openai/gpt-oss-20b
    #   llama-3.3-70b-versatile DEPRECATED 2026-08   -> openai/gpt-oss-120b
    #   mixtral-8x7b-32768     DEPRECATED 2025-03    -> gone
    # Do not "restore" the old names: they return 404, not a deprecation warning.
    GROQ_API_KEY: str = ""
    GROQ_MODEL_PRIMARY: str = "openai/gpt-oss-20b"
    GROQ_MODEL_FALLBACK: str = "openai/gpt-oss-120b"

    # ---- Google Gemini (fallback provider, free tier from AI Studio) ----
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL_PRIMARY: str = "gemini-2.5-flash-lite"

    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_TOKENS: int = 512

    # ---- HuggingFace Inference API (embeddings, free tier) ----
    HF_API_TOKEN: str = ""
    HF_EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    HF_API_BASE_URL: str = "https://api-inference.huggingface.co"
    HF_MAX_RETRIES: int = 3
    HF_RETRY_DELAY_SECONDS: float = 10.0

    # ---- Fallback: local embedding (disabled by default) ----
    USE_LOCAL_EMBEDDING_FALLBACK: bool = False

    # ---- Deduplication ----
    SIMILARITY_THRESHOLD: float = 0.85

    # ---- Environment ----
    # "development" | "production". Production enables the fail-fast checks in
    # validate_or_die() below.
    APP_ENV: str = "development"

    # ---- Internal trigger endpoint security ----
    #
    # SECURITY: this default is published in the repository, so if the env var
    # is unset in production the /internal/collect endpoint is "protected" by a
    # string anyone can read on GitHub. validate_or_die() refuses to start in
    # that state rather than logging a warning nobody reads.
    INTERNAL_SECRET: str = "change_this_to_a_random_secret_string"

    # ---- API ----
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # ---- CORS ----
    # Comma-separated list of allowed origins. Defaults to local dev only.
    # In production set this to the Vercel URL(s). A wildcard is NOT permitted
    # together with credentials — browsers reject that pair outright, and it
    # would expose authenticated admin routes to any origin. See
    # `allowed_origins` below for the enforcement.
    FRONTEND_URL: str = "http://localhost:3000,http://127.0.0.1:3000"
    CORS_ALLOW_CREDENTIALS: bool = True

    # ---- Article processing ----
    MAX_ARTICLE_TOKENS: int = 1024

    # ---- Article Fetcher ----
    ARTICLE_FETCH_TIMEOUT_SECONDS: int = 30
    MAX_ARTICLE_LENGTH_CHARS: int = 50000  # Truncate before cleaning to save memory
    FETCH_USER_AGENT: str = (
        "Mozilla/5.0 (compatible; NewsAggregator/1.0; +https://github.com/your-repo)"
    )

    # ---- Content Cleaner ----
    MIN_ARTICLE_WORD_COUNT: int = 50  # Skip articles shorter than this after cleaning

    # ---- LLM rate limiting and quota ----
    #
    # Groq free tier as of 2026-08-19: 30 RPM / 1,000 RPD / 8K TPM / 200K TPD.
    # REQUESTS-PER-DAY is the binding constraint, not requests-per-minute.
    # The old pipeline budgeted ~9,600 calls/day against this 1,000 ceiling;
    # see ARCHITECTURE_V3.md §A2 for the revised ~10 calls/day budget.
    GROQ_REQUESTS_PER_MINUTE: int = 25  # Stay under the 30 RPM free limit
    GROQ_INTER_REQUEST_DELAY_SECONDS: float = 2.5  # 60s / 25 RPM
    LLM_REQUESTS_PER_DAY: int = 1000  # Hard provider ceiling
    LLM_DAILY_CALL_BUDGET: int = 200  # Our self-imposed cap, well under the ceiling

    # ---- Categorization ----
    VALID_CATEGORIES: list = [
        "Politics",
        "Technology",
        "Business",
        "Science",
        "World",
        "Health",
        "Sports",
    ]
    MIN_CONFIDENCE_SCORE: float = 0.4  # Below this, assign category "Uncategorized"

    # ---- Storage conservation ----
    DELETE_RAW_HTML_AFTER_CLEANING: bool = True  # Conserve Neon 0.5 GB free limit
    DUPLICATE_WINDOW_ARTICLES: int = 100  # Compare against last N articles for dedup

    # ---- Rate limit overrides (domain -> requests per second) ----
    RATE_LIMIT_OVERRIDES: dict[str, float] = {}

    # ---- Derived helpers -------------------------------------------------

    @property
    def allowed_origins(self) -> list[str]:
        """Parsed CORS origin list.

        Splits ``FRONTEND_URL`` on commas and strips whitespace. A literal
        ``*`` is preserved here but is neutralised in ``main.py`` when
        credentials are enabled, because the two are mutually exclusive per
        the Fetch spec — browsers reject ``Access-Control-Allow-Origin: *``
        on a credentialed request, so the previous config was both insecure
        *and* non-functional.
        """
        return [origin.strip() for origin in self.FRONTEND_URL.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.strip().lower() in {"production", "prod"}

    def validate_or_die(self) -> None:
        """Refuse to start with insecure production configuration.

        Called from the FastAPI startup path. Deliberately raises rather than
        warns: a warning about an unset secret scrolls past in a deploy log and
        the service comes up anyway, exposed. Failing the deploy is louder and
        cheaper than the alternative.

        Only enforced when ``APP_ENV`` is production, so local development and
        the test suite are unaffected.
        """
        if not self.is_production:
            return

        problems: list[str] = []

        if self.INTERNAL_SECRET == "change_this_to_a_random_secret_string":
            problems.append(
                "INTERNAL_SECRET is still the default value published in the repo. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        elif len(self.INTERNAL_SECRET) < 24:
            problems.append("INTERNAL_SECRET is shorter than 24 characters.")

        if self.DATABASE_URL.startswith("sqlite"):
            problems.append(
                "DATABASE_URL is still the SQLite development default."
            )

        if "*" in self.allowed_origins:
            problems.append(
                "FRONTEND_URL is a wildcard. Set explicit origins in production."
            )

        if problems:
            raise RuntimeError(
                "Refusing to start — insecure production configuration:\n  - "
                + "\n  - ".join(problems)
            )

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
