"""Request-side middleware: rate limiting and crawler exclusion.

Both exist because this API is about to face the open internet. Neither is
sophisticated, and both are deliberately in-process.

SCOPE, STATED PLAINLY: the limiter's counters live in this process's memory.
With one Render instance that is exactly right. Behind the k8s manifests in
`k8s/`, each replica limits independently, so the effective ceiling is the
configured rate times the replica count. That is a known and acceptable
approximation — it still stops the runaway-script case it exists for, which is
the realistic threat, and the alternative is a Redis dependency the free tier
does not have. Revisit it when there is a shared cache for another reason.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("news.api.middleware")

DEFAULT_RATE = 60          # requests
DEFAULT_WINDOW = 60.0      # seconds
_MAX_TRACKED_CLIENTS = 10_000


class RateLimitMiddleware(BaseHTTPMiddleware):
    """A sliding-window limiter, keyed by client IP.

    The window is a deque of timestamps rather than a fixed bucket, because a
    fixed window lets a client spend its whole allowance at 0:59 and again at
    1:01 — twice the intended rate at the boundary, which is precisely when a
    runaway script hits.
    """

    def __init__(self, app, rate: int = DEFAULT_RATE, window: float = DEFAULT_WINDOW) -> None:
        super().__init__(app)
        self._rate = rate
        self._window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def _client_key(self, request: Request) -> str:
        # Render and Vercel both terminate TLS upstream, so the socket peer is
        # a proxy. The left-most X-Forwarded-For entry is the real client.
        # It is client-controlled and therefore spoofable — acceptable here,
        # because this limiter protects the free tier from accidents, not from
        # a determined attacker.
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            if len(self._hits) > _MAX_TRACKED_CLIENTS:
                # Unbounded growth is itself the denial of service. Drop the
                # windows that have fully expired; if that frees nothing, the
                # traffic is real and a flush is better than an OOM.
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > cutoff}
                if len(self._hits) > _MAX_TRACKED_CLIENTS:
                    logger.warning("rate limiter tracking table flushed under load")
                    self._hits.clear()

            window = self._hits.setdefault(key, deque())
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self._rate:
                return False
            window.append(now)
            return True

    async def dispatch(self, request: Request, call_next) -> Response:
        # Probes must never be rate limited: a limiter that starves the
        # readiness check turns a traffic spike into a pod restart loop.
        if request.url.path in ("/health", "/ready"):
            return await call_next(request)

        key = self._client_key(request)
        if not self._allow(key):
            logger.info("rate limited %s on %s", key, request.url.path)
            return JSONResponse(
                {"detail": "Too many requests."},
                status_code=429,
                headers={"Retry-After": str(int(self._window))},
            )
        return await call_next(request)


class NoIndexMiddleware(BaseHTTPMiddleware):
    """Assert `X-Robots-Tag: noindex` on every response.

    ROADMAP §1: this deployment is unlisted. The frontend carries its own
    meta tag, but a meta tag only covers HTML — it does nothing for a JSON
    endpoint, and `/articles` returns headlines that would otherwise be
    indexable directly. The header covers every response including those, and
    it is one line that cannot drift out of sync with a template.

    Delete this, the frontend meta tag and the robots.txt disallow together if
    the site ever goes public. Not one of them alone.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response
