"""URL canonicalisation and hashing.

Why this exists
---------------
``raw_articles.url`` was ``String(2048)`` with ``unique=True``. On PostgreSQL
that is a runtime error waiting to happen: a btree index entry is capped at
roughly 2,704 bytes, and a 2,048-*character* URL containing multi-byte UTF-8
(percent-decoded Japanese, Arabic or Cyrillic slugs are routine in this
corpus) exceeds it. The insert fails with
``index row size ... exceeds btree version 4 maximum``. See
ARCHITECTURE_V2.md §A4.

The fix is to index a fixed-width SHA-256 of the *canonical* URL instead.
64 hex characters, always, whatever the input.

Canonicalisation matters as much as hashing. These are the same article:

    https://example.com/story?utm_source=twitter&utm_medium=social
    https://example.com/story
    http://Example.com/story#comments
    https://example.com/story/

Hashing the raw string would store four copies. Canonicalising first
collapses them to one, which meaningfully reduces both the duplicate rate
and the storage pressure identified in §A6.
"""
from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Query parameters stripped during canonicalisation. Campaign and click
#: tracking only — never anything that selects content. ``id``, ``p``,
#: ``story``, ``article`` and friends are deliberately absent: dropping
#: those would collapse genuinely different articles into one hash, which
#: is a far worse failure than storing a duplicate.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        # Google / generic UTM
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "utm_name", "utm_reader", "utm_brand", "utm_social",
        "utm_social-type", "utm_swu",
        # Platform click identifiers
        "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "twclid",
        "igshid", "mc_cid", "mc_eid", "yclid", "ttclid", "rdt_cid",
        # Publisher-specific referral noise
        "ref", "referrer", "source", "src", "cmpid", "CMP", "ito", "at_medium",
        "at_campaign", "ncid", "smid", "partner", "sh", "__twitter_impression",
        "guccounter", "spm", "share_type",
    }
)

#: Default ports that carry no meaning and are removed.
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonicalize_url(url: str) -> str:
    """Return a canonical form of *url* for deduplication.

    Applied transformations, in order:

    1. Strip surrounding whitespace
    2. Lowercase the scheme and host (paths stay case-sensitive — many CMSes
       serve different content from ``/Story`` and ``/story``)
    3. Upgrade a bare ``http`` host to ``https`` **only for hashing purposes**,
       since publishers routinely serve both and redirect one to the other
    4. Drop the default port
    5. Remove tracking parameters, preserving the order of what remains
    6. Drop the fragment (``#comments`` is not a different article)
    7. Remove a single trailing slash from a non-empty path

    The result is not guaranteed to be fetchable — it is a dedup key. Always
    store the original URL separately for linking out to the publisher.

    Returns the input unchanged if it cannot be parsed.
    """
    if not url:
        return ""

    raw = url.strip()
    if not raw:
        return ""

    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    if not parts.netloc:
        return raw

    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"

    netloc = parts.netloc.lower()
    if "@" in netloc:  # strip any userinfo
        netloc = netloc.rsplit("@", 1)[-1]
    if ":" in netloc:
        host, _, port = netloc.rpartition(":")
        if port == _DEFAULT_PORTS.get(scheme) or port == "80":
            netloc = host

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    if parts.query:
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in {p.lower() for p in TRACKING_PARAMS}
        ]
        query = urlencode(kept)
    else:
        query = ""

    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    """Return the SHA-256 hex digest of the canonical form of *url*.

    Always 64 characters, so it indexes safely regardless of input length —
    which is the entire point (§A4).
    """
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()
