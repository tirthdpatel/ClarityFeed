"""The public rate limiter.

Tested against its own app with a deliberately tiny window rather than against
the real one. The suite raises the limit globally (see tests/conftest.py)
because a process-wide counter shared by hundreds of requests makes every
other test order-dependent — so the limiter needs somewhere to be exercised on
purpose.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.middleware import RateLimitMiddleware


def _app(rate: int = 3, window: float = 60.0) -> TestClient:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, rate=rate, window=window)

    @app.get("/articles")
    def articles() -> dict:
        return {"ok": True}

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return TestClient(app)


def test_requests_under_the_limit_pass() -> None:
    client = _app(rate=3)
    for _ in range(3):
        assert client.get("/articles").status_code == 200


def test_the_limit_is_enforced() -> None:
    client = _app(rate=3)
    for _ in range(3):
        client.get("/articles")
    assert client.get("/articles").status_code == 429


def test_a_limited_response_says_when_to_retry() -> None:
    client = _app(rate=1, window=30.0)
    client.get("/articles")
    blocked = client.get("/articles")
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "30"


def test_probes_are_never_limited() -> None:
    """A limiter that starves the readiness probe turns a traffic spike into a
    restart loop — the pod is killed for being popular."""
    client = _app(rate=1)
    for _ in range(20):
        assert client.get("/health").status_code == 200


def test_clients_are_counted_separately() -> None:
    client = _app(rate=2)
    for _ in range(2):
        client.get("/articles", headers={"x-forwarded-for": "10.0.0.1"})
    assert client.get("/articles", headers={"x-forwarded-for": "10.0.0.1"}).status_code == 429
    # A different client is unaffected by the first one's spending.
    assert client.get("/articles", headers={"x-forwarded-for": "10.0.0.2"}).status_code == 200


def test_the_leftmost_forwarded_address_identifies_the_client() -> None:
    """Render and Vercel terminate TLS upstream, so the socket peer is a proxy;
    the real client is the first entry in X-Forwarded-For."""
    client = _app(rate=1)
    client.get("/articles", headers={"x-forwarded-for": "10.0.0.5, 172.16.0.1"})
    same = client.get("/articles", headers={"x-forwarded-for": "10.0.0.5, 172.16.0.9"})
    assert same.status_code == 429, "different proxy hop was treated as a new client"
