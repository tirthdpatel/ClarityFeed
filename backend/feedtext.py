"""Turn feed-supplied HTML into the plain text an excerpt is supposed to be.

WHY THIS IS NOT BeautifulSoup

`backend/cleaner/text_cleaner.py` already strips markup, but it imports
BeautifulSoup and lxml, which are pipeline dependencies. The read API installs
requirements-api.txt and has neither, deliberately — see that file's header.
An excerpt cleaner that dragged a C-extension HTML parser into the API image
would undo that for the sake of removing some tags. `html.parser` is stdlib and
entirely adequate for a feed summary.

WHY IT MATTERS BEYOND LOOKS

The Guardian's RSS puts markup in `<description>`, so its excerpts rendered as
literal `<p>Follow the day's news live</p><ul><li>...` on the page. Worse, the
permission gate truncates to `max_description_chars`, and it was counting tag
characters as content: a 300-character budget spent on `<a href="...">` markup
leaves almost no sentence behind. Stripping has to happen BEFORE truncation for
the limit to mean what §11 says it means.
"""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

# Tags whose content is markup or styling rather than prose. Their text is
# dropped entirely rather than flattened into the excerpt.
_DROP_CONTENT = {"script", "style", "noscript", "iframe", "svg"}

# Block-level tags imply a boundary. Without this, "</p><p>" welds the last
# word of one paragraph onto the first of the next.
_BLOCK = {
    "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section", "article",
}

_WHITESPACE = re.compile(r"\s+")

#: Emitted where a block element ended, so the join between two blocks can be
#: chosen after the fact. A control character because it cannot occur in feed
#: text, so there is nothing to escape and nothing to collide with.
_BREAK = "\x00"

#: A block that already ends a sentence needs no help joining to the next one.
_SENTENCE_END = ".!?:;\u2026\u201d\"')]"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROP_CONTENT:
            self._suppress += 1
        elif tag in _BLOCK:
            self._parts.append(_BREAK)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT and self._suppress:
            self._suppress -= 1
        elif tag in _BLOCK:
            self._parts.append(_BREAK)

    def handle_data(self, data: str) -> None:
        if not self._suppress:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def strip_html(value: str | None) -> str | None:
    """Return `value` as plain text, or None if nothing survives.

    Never raises. A malformed excerpt must degrade to something readable rather
    than take down a page of articles, so a parse failure falls back to a blunt
    tag regex and then to the original string.
    """
    if not value:
        return value

    # Cheap exit: most sources send clean text and should not pay for a parse.
    if "<" not in value and "&" not in value:
        return value

    try:
        parser = _TextExtractor()
        parser.feed(value)
        parser.close()
        text = parser.text()
    except Exception:  # noqa: BLE001 - see docstring
        text = re.sub(r"<[^>]*>", " ", value)

    text = unescape(text)
    text = _join_blocks(text)
    text = _WHITESPACE.sub(" ", text).strip()

    # Feed summaries often trail into a truncation marker once the markup that
    # carried it is gone.
    text = re.sub(r"\s*(?:\.\.\.|…)\s*$", "…", text)

    return text or None


def _join_blocks(text: str) -> str:
    """Decide what goes between two blocks of prose.

    A space is wrong more often than it looks. The Guardian opens every item
    with a standfirst — a summary line carrying no terminal punctuation —
    followed by the body, so flattening `<p>A</p><p>B</p>` to "A B" produced
    "…spar over defence spending Alex Burghart, the deputy Tory leader…": two
    sentences welded into one that parses wrongly on the first read.

    An em dash rather than a full stop. A full stop would be inventing
    punctuation inside a publisher's words and claiming they wrote a sentence
    that ended there; a dash is visibly our join, and reads as one.

    Blocks that already end in sentence punctuation need nothing, and empty
    blocks — `<p></p>` is in every Guardian item — contribute no break at all.
    """
    out: list[str] = []

    for chunk in text.split(_BREAK):
        stripped = chunk.strip()
        if not stripped:
            # An empty block — `<p></p>` appears in every Guardian item — is a
            # spacer in the markup, not a boundary in the prose. It contributes
            # nothing, which also stops runs of them producing runs of dashes.
            continue

        if out:
            previous = out[-1].rstrip()
            already_ended = bool(previous) and previous[-1] in _SENTENCE_END
            out.append(" " if already_ended else " \u2014 ")

        out.append(stripped)

    return "".join(out)
