"""Extraction for saved HTML pages, kept strictly separate from chunking.

Session 13 section 8 states the rule this module implements: *extraction
decides what is content; chunking decides which content belongs together.*
Before this module `prepare_markdown` understood exactly one document type
(the arXiv abstract page) and returned every other input unchanged. A saved
HTML article therefore reached Rohan V2 with its navigation, cookie banner,
script bodies and footer intact, and those wrapper words were embedded as if
they were the author's argument.

Nothing here chooses a chunk boundary. The output is a normalized Markdown
rendering of the article region only; the suffix-rollover algorithm then runs
over that text unchanged.

Dependency-free by design: `html.parser` ships with CPython, so indexing a
real web page adds no supply-chain surface to the gateway.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Elements whose *text* is chrome, not content. Their entire subtree is
# dropped, which is why `script`/`style` bodies never reach an embedding.
BOILERPLATE_TAGS = frozenset({
    "script", "style", "noscript", "nav", "header", "footer", "aside", "form",
    "svg", "button", "select", "textarea", "iframe", "template", "dialog",
})
# Elements that end the current line of prose.
BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "main", "blockquote", "li", "ul", "ol",
    "table", "tr", "figure", "figcaption", "hr", "br", "dl", "dt", "dd",
})
HEADING_TAGS = {f"h{level}": level for level in range(1, 7)}
# Void elements never close, so they must never be pushed onto the tag stack.
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})
# A page that opens with one of these is HTML rather than Markdown that
# happens to mention a tag. Deliberately conservative: misclassifying prose
# as HTML would silently delete a user's document.
_HTML_SIGNALS = re.compile(r"<!doctype\s+html|<html[\s>]|<body[\s>]|<article[\s>]|<main[\s>]", re.IGNORECASE)
_ROOT_PATTERN = re.compile(r"<(article|main)[\s>]", re.IGNORECASE)


def looks_like_html(text: str) -> bool:
    """True only for documents that clearly announce themselves as HTML."""
    return bool(_HTML_SIGNALS.search(text[:4000]))


def is_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    """True for markup a sighted reader would never see.

    Text hidden with ``display:none``, ``visibility:hidden``, ``hidden`` or
    ``aria-hidden`` is not content: on a crawled page it is keyword stuffing or
    an instruction aimed at whatever machine reads the page. Indexing it would
    let a third-party document place text into an answer worker's evidence
    that no human reviewing that page could see.
    """
    values = {key.lower(): (value or "") for key, value in attrs}
    if "hidden" in values:
        return True
    if values.get("aria-hidden", "").strip().lower() == "true":
        return True
    style = values.get("style", "").lower().replace(" ", "")
    return "display:none" in style or "visibility:hidden" in style


class _ArticleExtractor(HTMLParser):
    """Collects the article region as Markdown, dropping boilerplate subtrees.

    When the page exposes an ``<article>`` or ``<main>`` landmark, only that
    subtree is captured -- the same decision a reader makes when they ignore
    the sidebar. Otherwise the whole body is captured minus boilerplate.

    A tag *stack* rather than a counter, because the dropped element may be an
    ordinary ``<div>``: a counter keyed on element name cannot tell which
    ``</div>`` ends the hidden subtree and would swallow the rest of the page.
    """

    def __init__(self, *, root: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.root = root
        self.parts: list[str] = []
        self._open: list[str] = []
        self._drop_at: int | None = None   # stack depth where dropping began
        self._heading: int | None = None
        self._in_pre = False
        self._dropped_words = 0

    # -- state helpers -------------------------------------------------
    @property
    def _capturing(self) -> bool:
        if self._drop_at is not None:
            return False
        return self.root in self._open if self.root else True

    def _emit(self, text: str) -> None:
        self.parts.append(text)

    def _break(self, marker: str = "\n\n") -> None:
        if self.parts and not self.parts[-1].endswith(marker):
            self._emit(marker)

    # -- parser callbacks ----------------------------------------------
    def handle_startendtag(self, tag: str, attrs) -> None:
        # An XHTML-style <div/> opens and closes at once; never stack it.
        if tag.lower() == "br" and self._capturing:
            self._break()

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        drop = tag in BOILERPLATE_TAGS or is_hidden(attrs)
        if tag in VOID_TAGS:
            if tag == "br" and self._capturing:
                self._break()
            return
        self._open.append(tag)
        if drop and self._drop_at is None:
            self._drop_at = len(self._open) - 1
            return
        if not self._capturing:
            return
        if tag in HEADING_TAGS:
            self._break()
            self._heading = HEADING_TAGS[tag]
            self._emit("#" * self._heading + " ")
        elif tag == "pre":
            self._break()
            self._in_pre = True
            self._emit("```\n")
        elif tag == "li":
            self._break()
            self._emit("- ")
        elif tag in BLOCK_TAGS:
            self._break()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        if tag in self._open:
            # Unwind to the matching open tag, tolerating unclosed children
            # the way a browser does.
            depth = len(self._open) - 1 - self._open[::-1].index(tag)
            del self._open[depth:]
            if self._drop_at is not None and depth <= self._drop_at:
                self._drop_at = None
                return
        if self._drop_at is not None:
            return
        if tag in HEADING_TAGS and self._heading:
            self._heading = None
            self._break()
        elif tag == "pre" and self._in_pre:
            self._in_pre = False
            self._emit("\n```\n\n")
        elif tag in BLOCK_TAGS:
            self._break()

    def handle_data(self, data: str) -> None:
        if self._drop_at is not None:
            self._dropped_words += len(data.split())
            return
        if not self._capturing:
            return
        if self._in_pre:
            self._emit(data)
            return
        # Collapse HTML's insignificant whitespace, but keep the words exactly.
        collapsed = re.sub(r"\s+", " ", data)
        if not collapsed.strip():
            if self.parts and not self.parts[-1].endswith((" ", "\n")):
                self._emit(" ")
            return
        if self._heading:
            collapsed = collapsed.strip()
        self._emit(collapsed)

    def result(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" ?\n ?", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def extract_html(source: str) -> tuple[str, dict[str, object]]:
    """Return (markdown, stats) for a saved HTML page.

    ``stats`` is provenance, not decoration: a caller can see which landmark
    was used and how many wrapper words were excluded, so a bad extraction is
    visible in the manifest instead of silently degrading retrieval.
    """
    root_match = _ROOT_PATTERN.search(source)
    root = root_match.group(1).lower() if root_match else None
    parser = _ArticleExtractor(root=root)
    parser.feed(source)
    parser.close()
    markdown = parser.result()
    if not markdown.strip() and root:
        # A landmark existed but held nothing usable (JS-rendered shell).
        # Retry across the whole body rather than indexing an empty document.
        parser = _ArticleExtractor(root=None)
        parser.feed(source)
        parser.close()
        markdown = parser.result()
        root = None
    return markdown, {
        "extractor": "html_article",
        "landmark": root or "body",
        "boilerplate_words_dropped": parser._dropped_words,
        "extracted_words": len(markdown.split()),
    }
