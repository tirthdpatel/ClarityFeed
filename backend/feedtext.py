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


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROP_CONTENT:
            self._suppress += 1
        elif tag in _BLOCK:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT and self._suppress:
            self._suppress -= 1
        elif tag in _BLOCK:
            self._parts.append(" ")

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
    text = _WHITESPACE.sub(" ", text).strip()

    # Feed summaries often trail into a truncation marker once the markup that
    # carried it is gone.
    text = re.sub(r"\s*(?:\.\.\.|…)\s*$", "…", text)

    return text or None
