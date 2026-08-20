"""Tests for the permission gate — ARCHITECTURE_V2 §3.6 / §11.

This is the legal core, so the tests are written around the question a
publisher's lawyer would actually ask: *what did you store, and what did you
show?*
"""
from __future__ import annotations

import pytest

from backend.permissions import (
    GateResult, Permissions, apply_ingest_gate, apply_serialize_gate,
    may_summarize, may_translate,
)

ARTICLE = {
    "title": "A headline",
    "description": "d" * 500,
    "full_text": "the entire article body",
    "image_url": "https://publisher.example/img.jpg",
    "url": "https://publisher.example/article",
}


class TestRestrictiveDefaults:
    def test_unreviewed_source_gets_metadata_only(self):
        r = apply_ingest_gate(ARTICLE, Permissions.restrictive())
        assert r.fields["full_text"] is None
        assert r.fields["image_url"] is None
        assert r.fields["title"] == "A headline"
        assert r.fields["url"] == ARTICLE["url"]

    def test_missing_permission_row_is_restrictive_not_permissive(self):
        """A source with no row must not be treated as fully licensed."""
        p = Permissions.from_row(None)
        assert p.can_store_full_text is False
        assert p.can_store_image is False
        assert p.reviewed is False

    def test_description_truncated_to_limit(self):
        r = apply_ingest_gate(ARTICLE, Permissions.restrictive())
        assert len(r.fields["description"]) <= 301  # 300 + ellipsis
        assert "description" in r.truncated

    def test_truncation_breaks_on_a_word(self):
        art = {**ARTICLE, "description": " ".join(["word"] * 200)}
        r = apply_ingest_gate(art, Permissions(max_description_chars=50))
        assert not r.fields["description"].replace("…", "").endswith("wor")

    def test_url_is_never_dropped(self):
        """A link is not a reproduction, and without it there is no
        attribution and no route back to the publisher."""
        harsh = Permissions(
            can_store_title=False, can_store_description=False,
            can_store_full_text=False, can_store_image=False,
        )
        r = apply_ingest_gate(ARTICLE, harsh)
        assert r.fields["url"] == ARTICLE["url"]


class TestLicensedSource:
    def test_full_text_kept_when_permitted(self):
        p = Permissions(can_store_full_text=True, can_store_image=True,
                        max_description_chars=1000, reviewed=True)
        r = apply_ingest_gate(ARTICLE, p)
        assert r.fields["full_text"] == ARTICLE["full_text"]
        assert r.fields["image_url"] == ARTICLE["image_url"]
        assert r.dropped == []


class TestSerializeGate:
    def test_revoked_permission_applies_without_recrawl(self):
        """Permissions change. A publisher who withdraws consent must stop
        being reproduced on the next request, not the next crawl."""
        stored = {"content": "full body", "imageUrl": "https://i/x.jpg",
                  "summary": {"tldr": "..."}, "url": ARTICLE["url"]}
        out = apply_serialize_gate(stored, Permissions.restrictive())
        assert out["content"] is None
        assert out["imageUrl"] is None

    def test_attribution_block_is_always_present(self):
        out = apply_serialize_gate({"url": ARTICLE["url"]}, Permissions.restrictive())
        assert out["attribution"]["required"] is True
        assert out["attribution"]["readOriginalUrl"] == ARTICLE["url"]

    def test_summary_removed_when_not_permitted(self):
        out = apply_serialize_gate(
            {"summary": {"tldr": "x"}, "url": "u"},
            Permissions(can_generate_summary=False),
        )
        assert out["summary"] is None


class TestDerivedPermissions:
    def test_translation_allowed_by_default(self):
        assert may_translate(Permissions.restrictive()) is True

    def test_translation_revocable_independently(self):
        assert may_translate(Permissions(can_translate=False)) is False

    def test_cannot_translate_what_cannot_be_stored(self):
        assert may_translate(Permissions(can_store_title=False)) is False

    def test_summary_permission(self):
        assert may_summarize(Permissions.restrictive()) is True
        assert may_summarize(Permissions(can_generate_summary=False)) is False
