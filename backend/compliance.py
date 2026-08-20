"""
Legal and ethical compliance layer for ClarityFeed.

This module provides gating mechanisms that every data collection operation
must pass through before making external requests. It enforces:

1. robots.txt compliance via RobotsTxtChecker
2. Per-domain rate limiting via RateLimiter
3. HTTP conditional request headers via ConditionalRequestHeaders
4. Source attribution validation via validate_attribution()

This module is a pure dependency with no circular imports.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

logger = logging.getLogger("news.compliance")


# ---------------------------------------------------------------------------
# robots.txt compliance
# ---------------------------------------------------------------------------

# Module-level cache: { base_url: (RobotFileParser, fetch_timestamp) }
_robots_cache: dict[str, tuple[RobotFileParser, float]] = {}
_ROBOTS_TTL_SECONDS: float = 3600.0

# Identify ourselves honestly and give a way to be blocked. A crawler that
# hides behind a browser UA cannot be excluded by a publisher who wants to
# exclude it, which makes the robots.txt check below theatre.
ROBOTS_USER_AGENT: str = "ClarityFeedBot/1.0 (+https://github.com/clarityfeed)"
ROBOTS_TIMEOUT_SECONDS: float = 10.0


class RobotsTxtChecker:
    """Checks whether a URL is allowed according to the site's robots.txt.

    Fetches and parses robots.txt with a configurable TTL (default 3600s).
    Since the Render service may cold-start and lose the in-memory cache,
    the first request after a cold start simply re-fetches robots.txt.
    This is acceptable and expected behaviour.
    """

    def __init__(
        self,
        base_url: str,
        ttl: float = _ROBOTS_TTL_SECONDS,
        fail_closed: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._ttl = ttl
        self._fail_closed = fail_closed

    # -- public ---------------------------------------------------------------

    def is_allowed(self, url: str, user_agent: str = "*") -> bool:
        """Return True if *url* may be fetched according to robots.txt.

        FAIL-CLOSED (ARCHITECTURE_V2 §A9). If robots.txt cannot be retrieved
        or parsed, this returns ``False`` — we do not fetch.

        The previous behaviour returned ``True`` on any failure, which meant
        a transient DNS blip or a publisher's WAF rejecting our user-agent
        silently escalated us from "compliant crawler" to "crawler ignoring
        robots.txt". That is exactly the posture this project cannot afford,
        and it failed open precisely when a site was least happy to see us.

        Note the distinction this method preserves:

        * robots.txt returns **404** — the standard says no restrictions.
          ``RobotFileParser.read()`` sets ``allow_all`` and we honour it.
          Absent is not the same as unknown.
        * robots.txt is **unreachable** (DNS, timeout, connection reset) —
          the rules are unknown, so we decline.

        It never raises.
        """
        parser = self._get_parser()
        if parser is None:
            if self._fail_closed:
                logger.warning(
                    "robots.txt unavailable for %s — DENYING fetch of %s (fail-closed)",
                    self._base_url,
                    url,
                )
                return False
            logger.warning(
                "robots.txt unavailable for %s — allowing %s (fail_closed disabled)",
                self._base_url,
                url,
            )
            return True
        try:
            return parser.can_fetch(user_agent, url)
        except Exception:
            logger.warning("robots.txt parse error for %s — denying", url, exc_info=True)
            return not self._fail_closed

    # -- private --------------------------------------------------------------

    def _get_parser(self) -> RobotFileParser | None:
        """Return a cached or freshly-fetched RobotFileParser."""
        now = time.time()
        cached = _robots_cache.get(self._base_url)
        if cached is not None:
            parser, ts = cached
            if now - ts < self._ttl:
                logger.debug("robots.txt cache hit for %s", self._base_url)
                return parser

        return self._fetch_and_cache()

    def _fetch_and_cache(self) -> RobotFileParser | None:
        """Fetch robots.txt and store the parsed result in the module cache.

        This deliberately does NOT use ``RobotFileParser.read()``. That method
        calls ``urllib.request.urlopen``, which has two problems in production:

        1. It verifies TLS against the *interpreter's* CA store rather than
           certifi. A python.org framework build on macOS ships that store
           empty until ``Install Certificates.command`` is run, so every
           robots.txt fetch raises CERTIFICATE_VERIFY_FAILED — and because
           this checker is fail-closed, that silently denies every source.
           The symptom is an ingestion run reporting "empty feed" for
           everything, which looks like dead feeds, not a TLS problem.
        2. It sends urllib's default User-Agent, which a number of publishers
           block outright. Being denied by the very file that tells us what
           we may fetch is a poor way to introduce ourselves.

        ``requests`` fixes both: certifi by default, and a UA we control.
        Status handling follows RFC 9309 §2.3.1, matching what ``read()``
        does internally — 4xx other than 401/403 means "no restrictions",
        401/403 means "everything is off limits", and anything else is an
        error and therefore a denial.
        """
        robots_url = f"{self._base_url}/robots.txt"
        logger.info("Fetching robots.txt from %s", robots_url)
        try:
            import requests

            parser = RobotFileParser()
            parser.set_url(robots_url)
            resp = requests.get(
                robots_url,
                headers={"User-Agent": ROBOTS_USER_AGENT},
                timeout=ROBOTS_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            if resp.status_code in (401, 403):
                parser.disallow_all = True
            elif 400 <= resp.status_code < 500:
                parser.allow_all = True
            elif resp.status_code >= 500:
                raise RuntimeError(
                    f"robots.txt returned HTTP {resp.status_code}"
                )
            else:
                parser.parse(resp.text.splitlines())

            parser.modified()
            _robots_cache[self._base_url] = (parser, time.time())
            return parser
        except Exception:
            logger.warning(
                "Failed to fetch robots.txt from %s — fetch will be denied (fail-closed)",
                robots_url,
                exc_info=True,
            )
            return None


# ---------------------------------------------------------------------------
# Per-domain rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Per-domain token-bucket rate limiter.

    Default: 1 request/second per domain with a burst allowance of 3.

    Uses ``asyncio.sleep`` when tokens are unavailable. Configuration is
    injectable from ``settings.RATE_LIMIT_OVERRIDES`` (dict mapping domain
    string to requests-per-second float).

    Note: this in-memory rate limiter operates **per-process**. For the
    free single-instance Render deployment, this is sufficient. If multiple
    workers are introduced in the future, a shared (e.g. Redis-based)
    limiter would be needed.
    """

    def __init__(
        self,
        default_rps: float = 1.0,
        burst: int = 3,
        overrides: dict[str, float] | None = None,
    ) -> None:
        self._default_rps = default_rps
        self._burst = burst
        self._overrides: dict[str, float] = overrides or {}
        # { domain: [tokens, last_refill_timestamp] }
        self._buckets: dict[str, list[float]] = {}

    async def acquire(self, url: str) -> None:
        """Wait until a token is available for the domain of *url*."""
        domain = urlparse(url).netloc
        rps = self._overrides.get(domain, self._default_rps)
        bucket = self._buckets.setdefault(domain, [float(self._burst), time.time()])

        # Refill tokens based on elapsed time
        now = time.time()
        elapsed = now - bucket[1]
        bucket[0] = min(float(self._burst), bucket[0] + elapsed * rps)
        bucket[1] = now

        if bucket[0] < 1.0:
            wait = (1.0 - bucket[0]) / rps
            logger.debug("Rate limiter: sleeping %.2fs for %s", wait, domain)
            await asyncio.sleep(wait)
            bucket[0] = 0.0
            bucket[1] = time.time()
        else:
            bucket[0] -= 1.0


# ---------------------------------------------------------------------------
# Conditional request headers (ETag / Last-Modified)
# ---------------------------------------------------------------------------


class ConditionalRequestHeaders:
    """In-memory store mapping URLs to ETag and Last-Modified header values.

    This enables HTTP conditional requests (``If-None-Match`` /
    ``If-Modified-Since``) which reduce bandwidth and respect publisher
    caching directives.

    Note: the cache is in-memory only. Render cold starts will reset it,
    causing one unconditional fetch per RSS source after each cold start.
    This is acceptable and expected behaviour.
    """

    def __init__(self) -> None:
        # { url: {"etag": str | None, "last_modified": str | None} }
        self._store: dict[str, dict[str, str | None]] = {}

    def get_headers(self, url: str) -> dict[str, str]:
        """Return conditional request headers for *url* (may be empty)."""
        entry = self._store.get(url)
        if entry is None:
            return {}

        headers: dict[str, str] = {}
        if entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]
        if entry.get("last_modified"):
            headers["If-Modified-Since"] = entry["last_modified"]
        return headers

    def update(self, url: str, response_headers: dict[str, str]) -> None:
        """Store ETag / Last-Modified values from *response_headers*."""
        etag = response_headers.get("ETag") or response_headers.get("etag")
        last_mod = (
            response_headers.get("Last-Modified")
            or response_headers.get("last-modified")
        )
        if etag or last_mod:
            self._store[url] = {"etag": etag, "last_modified": last_mod}


# ---------------------------------------------------------------------------
# Attribution validation
# ---------------------------------------------------------------------------


def validate_attribution(article: dict[str, object]) -> bool:
    """Check that *article* carries required attribution fields.

    Required fields: ``url``, ``source_name``, ``title``.
    Returns False and logs a WARNING for any missing / empty field.
    """
    required = ("url", "source_name", "title")
    valid = True
    for field in required:
        value = article.get(field)
        if not value or (isinstance(value, str) and not value.strip()):
            logger.warning(
                "Attribution missing or empty field '%s' in article: %s",
                field,
                article.get("url", "<unknown>"),
            )
            valid = False
    return valid
