"""
Article fetcher with three-layer extraction fallback.

Fetches full article HTML for PENDING raw_articles, extracts body text using
newspaper3k → readability-lxml → BeautifulSoup fallback, and stores cleaned
text in cleaned_articles. Respects robots.txt and per-domain rate limiting.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from newspaper import Article
from readability import Document
from sqlalchemy.orm import Session

from backend.compliance import RateLimiter, RobotsTxtChecker
from backend.database.models import PipelineStatus, RawArticleRecord
from backend.database.orm_models import CleanedArticle, RawArticle
from config.settings import settings

logger = logging.getLogger("news.fetcher.article_fetcher")


@dataclass
class FetchBatchResult:
    """Result of a batch fetch-and-extract operation."""

    fetched: int
    extracted: int
    failed: int
    skipped: int  # articles already in cleaned_articles (idempotency)
    duration_seconds: float


class ArticleFetcher:
    """Fetches article HTML and extracts body text using a three-layer fallback chain."""

    def __init__(self) -> None:
        self._rate_limiter = RateLimiter(overrides=dict(settings.RATE_LIMIT_OVERRIDES))

    async def fetch_and_extract_batch(
        self,
        db: Session,
        articles: List[RawArticleRecord],
        max_concurrent: Optional[int] = None,
    ) -> FetchBatchResult:
        """Fetch HTML and extract body text for a batch of articles.

        Uses asyncio.Semaphore to cap concurrency. Respects robots.txt and
        per-domain rate limiting. Idempotent: skips articles already in
        cleaned_articles.
        """
        max_concurrent = max_concurrent or settings.MAX_CONCURRENT_FETCHES
        sem = asyncio.Semaphore(max_concurrent)
        start = time.monotonic()

        fetched = 0
        extracted = 0
        failed = 0
        skipped = 0

        for article in articles:
            if article.id is None:
                continue

            # Idempotency: skip if already in cleaned_articles
            existing = db.query(CleanedArticle).filter(
                CleanedArticle.raw_article_id == article.id
            ).first()
            if existing is not None:
                skipped += 1
                continue

            # robots.txt check before any HTTP request
            base_url = self._base_url(article.url)
            checker = RobotsTxtChecker(base_url)
            user_agent = settings.FETCH_USER_AGENT
            if not checker.is_allowed(article.url, user_agent=user_agent):
                logger.warning("robots.txt disallows %s — skipping", article.url)
                continue

            try:
                await self._rate_limiter.acquire(article.url)
            except Exception:
                logger.warning("Rate limiter acquire failed for %s", article.url)
                continue

            async with sem:
                html: Optional[str] = None
                try:
                    async with httpx.AsyncClient(
                        timeout=settings.ARTICLE_FETCH_TIMEOUT_SECONDS,
                        headers={"User-Agent": user_agent},
                    ) as client:
                        resp = await client.get(article.url)
                        fetched += 1
                        if resp.status_code >= 400:
                            logger.warning(
                                "HTTP %d for %s — marking FAILED",
                                resp.status_code,
                                article.url,
                            )
                            self._mark_failed(db, article.id)
                            failed += 1
                            continue
                        html = resp.text
                except (httpx.RequestError, httpx.TimeoutException) as e:
                    logger.warning("Fetch error for %s: %s — marking FAILED", article.url, e)
                    self._mark_failed(db, article.id)
                    failed += 1
                    continue
                except Exception as e:
                    logger.error("Unexpected fetch error for %s: %s", article.url, e, exc_info=True)
                    self._mark_failed(db, article.id)
                    failed += 1
                    continue

                if not html:
                    self._mark_failed(db, article.id)
                    failed += 1
                    continue

                body = self._extract_body(html, article.url)
                if body is None:
                    logger.warning("extraction_failed for %s — marking FAILED", article.url)
                    self._mark_failed(db, article.id)
                    failed += 1
                    continue

                # Truncation: Groq context window is finite and Neon storage is limited
                if len(body) > settings.MAX_ARTICLE_LENGTH_CHARS:
                    body = body[: settings.MAX_ARTICLE_LENGTH_CHARS]

                word_count = len(body.split())
                if word_count < settings.MIN_ARTICLE_WORD_COUNT:
                    logger.warning(
                        "Extracted text too short (%d words) for %s — marking FAILED",
                        word_count,
                        article.url,
                    )
                    self._mark_failed(db, article.id)
                    failed += 1
                    continue

                try:
                    cleaned = CleanedArticle(
                        raw_article_id=article.id,
                        clean_text=body,
                        word_count=word_count,
                    )
                    db.add(cleaned)
                    db.flush()

                    if settings.DELETE_RAW_HTML_AFTER_CLEANING:
                        db.query(RawArticle).filter(RawArticle.id == article.id).update(
                            {"raw_html": None}
                        )
                    db.commit()
                    extracted += 1
                    logger.debug("Extracted %d words for %s", word_count, article.url)
                except Exception as e:
                    db.rollback()
                    logger.error("Failed to insert cleaned article %s: %s", article.url, e)
                    self._mark_failed(db, article.id)
                    failed += 1

        duration = time.monotonic() - start
        return FetchBatchResult(
            fetched=fetched,
            extracted=extracted,
            failed=failed,
            skipped=skipped,
            duration_seconds=round(duration, 2),
        )

    def _extract_body(self, html: str, url: str) -> Optional[str]:
        """Extract article body using three-layer fallback chain."""
        # Layer 1 — newspaper (newspaper4k)
        #
        # newspaper3k (2020, unmaintained) does not build against modern
        # setuptools — its jieba3k / tinysegmenter / feedfinder2 dependencies
        # fail outright, which blocked `pip install -r requirements.txt`
        # entirely. newspaper4k is the maintained fork, but it renamed
        # `set_html(html)` to `download(input_html=html)`. It is also a
        # slightly less aggressive extractor on thin markup — the readability
        # and BeautifulSoup layers below absorb that.
        try:
            article = Article(url)
            article.download(input_html=html)
            article.parse()
            text = article.text or ""
            if text and len(text.split()) >= settings.MIN_ARTICLE_WORD_COUNT:
                return text.strip()
        except Exception as e:
            logger.debug("newspaper extraction failed for %s: %s", url, e)

        # Layer 2 — readability-lxml
        try:
            doc = Document(html)
            summary_html = doc.summary()
            if summary_html:
                soup = BeautifulSoup(summary_html, "lxml")
                text = soup.get_text(separator=" ").strip()
                if text and len(text.split()) >= settings.MIN_ARTICLE_WORD_COUNT:
                    return text
        except Exception as e:
            logger.debug("readability extraction failed for %s: %s", url, e)

        # Layer 3 — BeautifulSoup brute-force
        try:
            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            paragraphs = soup.find_all("p")
            text = " ".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))
            if text and len(text.split()) >= settings.MIN_ARTICLE_WORD_COUNT:
                return text.strip()
        except Exception as e:
            logger.debug("BeautifulSoup extraction failed for %s: %s", url, e)

        return None

    @staticmethod
    def _base_url(url: str) -> str:
        """Extract base URL (scheme + netloc) from full URL."""
        parsed = urlparse(url)
        return f"{parsed.scheme or 'https'}://{parsed.netloc}"

    @staticmethod
    def _mark_failed(db: Session, raw_article_id: int) -> None:
        """Mark raw_article as FAILED."""
        try:
            db.query(RawArticle).filter(RawArticle.id == raw_article_id).update(
                {"status": PipelineStatus.FAILED}
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
