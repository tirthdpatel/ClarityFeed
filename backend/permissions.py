"""The permission gate — what a publisher lets us keep and show.

ARCHITECTURE_V2 §3.6 and §11. This is the legal core of the product, and it
works by *dropping fields at the boundary* rather than by remembering to check
a flag at every call site. Two boundaries matter:

    INGEST      what gets written to the database at all
    SERIALIZE   what leaves the API in a response

Doing it at ingest alone would be wrong — permissions change, and a publisher
who revokes full-text consent must stop being reproduced immediately, not
after the next re-crawl. Doing it at serialize alone would be worse: we would
be holding text we were never allowed to store.

Every default is restrictive. A source with no reviewed permission row can
contribute a headline, a short excerpt and a link. Nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Permissions:
    """A resolved permission set. Defaults match the DB defaults exactly."""

    can_store_title: bool = True
    can_store_description: bool = True
    can_store_full_text: bool = False
    can_store_image: bool = False
    can_generate_summary: bool = True
    can_translate: bool = True
    requires_attribution: bool = True
    original_url_required: bool = True
    max_description_chars: int = 300
    robots_override: bool = False
    reviewed: bool = False

    @classmethod
    def restrictive(cls) -> "Permissions":
        """What an unreviewed source gets. Deliberately the same as the
        dataclass defaults — there is no separate 'unknown' posture to drift
        out of sync."""
        return cls()

    @classmethod
    def from_row(cls, row: Any | None) -> "Permissions":
        if row is None:
            return cls.restrictive()
        return cls(
            can_store_title=bool(row.can_store_title),
            can_store_description=bool(row.can_store_description),
            can_store_full_text=bool(row.can_store_full_text),
            can_store_image=bool(row.can_store_image),
            can_generate_summary=bool(row.can_generate_summary),
            can_translate=bool(row.can_translate),
            requires_attribution=bool(row.requires_attribution),
            original_url_required=bool(row.original_url_required),
            max_description_chars=int(row.max_description_chars),
            robots_override=bool(row.robots_override),
            reviewed=row.reviewed_at is not None,
        )


@dataclass
class GateResult:
    """What survived the gate, and what was removed."""

    fields: dict[str, Any]
    dropped: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.dropped or self.truncated)


def apply_ingest_gate(article: dict[str, Any], perms: Permissions) -> GateResult:
    """Drop fields the source does not licence, before anything is stored.

    `url` is never dropped: a link is not a reproduction, and without it there
    is no attribution and no way for a reader to reach the publisher.
    """
    out = dict(article)
    dropped: list[str] = []
    truncated: list[str] = []

    if not perms.can_store_title:
        # A source that forbids even the headline should not be ingested at
        # all; surfacing it as a dropped field makes that visible in admin
        # rather than producing silently empty articles.
        if out.get("title"):
            dropped.append("title")
        out["title"] = ""

    if not perms.can_store_description:
        if out.get("description"):
            dropped.append("description")
        out["description"] = None
    elif out.get("description") and perms.max_description_chars > 0:
        desc = out["description"]
        if len(desc) > perms.max_description_chars:
            # Cut on a word boundary — a hard slice mid-word reads as broken.
            cut = desc[: perms.max_description_chars]
            if " " in cut:
                cut = cut[: cut.rfind(" ")]
            out["description"] = cut.rstrip() + "…"
            truncated.append("description")

    if not perms.can_store_full_text:
        if out.get("full_text"):
            dropped.append("full_text")
        out["full_text"] = None

    if not perms.can_store_image:
        if out.get("image_url"):
            dropped.append("image_url")
        out["image_url"] = None

    return GateResult(fields=out, dropped=dropped, truncated=truncated)


def apply_serialize_gate(article: dict[str, Any], perms: Permissions) -> dict[str, Any]:
    """Shape an API response according to current permissions.

    Re-applied on the way out because permissions change after ingest. A
    publisher who revokes consent stops being reproduced on the next request,
    not on the next crawl.
    """
    out = dict(article)

    if not perms.can_store_full_text:
        out["content"] = None
    if not perms.can_store_image:
        out["imageUrl"] = None
    if not perms.can_generate_summary:
        out["summary"] = None

    if perms.requires_attribution or perms.original_url_required:
        out["attribution"] = {
            "required": perms.requires_attribution,
            "readOriginalUrl": article.get("url"),
        }
    return out


def may_summarize(perms: Permissions) -> bool:
    return perms.can_generate_summary


def may_translate(perms: Permissions) -> bool:
    """Translation is a derivative work (V3 §B8). Allowed by default because
    it sits closer to summarisation than to reproduction, but a publisher can
    revoke it independently — and revoking summaries revokes translation of
    them too, since a translated summary is still a summary."""
    return perms.can_translate and perms.can_store_title
