# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Turn INPS rich-text HTML into numbered, citable paragraphs.

Every paragraph gets a stable anchor ``<section>.p<n>`` (e.g. ``4.p2``) so that
an extracted rule or an answer can cite exactly where its words come from, and
code — not the model — can check that the quoted words are really there.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote

_BLOCK = {"p", "li", "tr", "div", "h1", "h2", "h3", "h4", "br"}
_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+\S")


@dataclass(frozen=True)
class Paragraph:
    anchor: str  # "4.p2"
    section: str  # "4" ("0" = text before the first heading)
    heading: str  # section title, "" before the first heading
    text: str


def decode_field(value: str | None) -> str:
    """INPS JSON fields are percent-encoded HTML; return decoded HTML."""
    return unquote(value or "")


class _Blocks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, bool]] = []  # (text, is_bold_only)
        self._buf: list[str] = []
        self._bold_depth = 0
        self._bold_chars = 0
        self._cell_sep = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("strong", "b"):
            self._bold_depth += 1
        elif tag == "td":
            self._cell_sep = True
        elif tag in _BLOCK:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("strong", "b"):
            self._bold_depth = max(0, self._bold_depth - 1)
        elif tag == "td":
            self._buf.append(" | ")
        elif tag in _BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._cell_sep:
            self._cell_sep = False
        self._buf.append(data)
        if self._bold_depth:
            self._bold_chars += len(data.strip())

    def _flush(self) -> None:
        text = normalize_space("".join(self._buf)).strip(" |")
        if text:
            self.blocks.append((text, self._bold_chars >= len(text.replace(" ", "")) * 0.8))
        self._buf, self._bold_chars = [], 0

    def close(self) -> None:
        super().close()
        self._flush()


def normalize_space(s: str) -> str:
    s = html.unescape(s).replace("\xa0", " ")
    s = unicodedata.normalize("NFC", s)
    return re.sub(r"\s+", " ", s).strip()


def paragraphs(html_text: str) -> list[Paragraph]:
    """Split HTML into paragraphs anchored to their numbered section.

    The table of contents ("INDICE") repeats the headings without bold; real
    headings in INPS circulars are bold, so only bold numbered lines start a
    section. Everything before the first real heading belongs to section "0".
    """
    parser = _Blocks()
    parser.feed(html_text)
    parser.close()
    out: list[Paragraph] = []
    section, heading = "0", ""
    counters: dict[str, int] = {}  # a section number can repeat (annexes): keep counting, never reuse an anchor
    for text, bold in parser.blocks:
        m = _HEADING.match(text)
        if bold and m and len(text) < 220:
            section, heading = m.group(1), text
            continue
        if section == "0" and (text.upper() == "INDICE" or m):
            continue  # table of contents: repeats the headings without bold
        n = counters[section] = counters.get(section, 0) + 1
        out.append(Paragraph(f"{section}.p{n}", section, heading, text))
    return out


def content_hash(text: str) -> str:
    """Hash of the normalized text: the identity of one *version* of a document."""
    return hashlib.sha256(normalize_space(text).encode("utf-8")).hexdigest()


MIN_QUOTE_WORDS = 4


def canon(s: str) -> str:
    """Lowercase words separated by single spaces; apostrophes and quote marks become spaces."""
    s = normalize_space(s).lower()
    return re.sub(r"[^\w%€]+", " ", s).strip()


def quote_found(quote: str, source: str, min_words: int = MIN_QUOTE_WORDS) -> bool:
    """True when ``quote`` occurs in ``source`` as a run of whole words.

    This is the anti-hallucination gate: a citation counts only if at least
    ``min_words`` of its words appear, in order and on word boundaries, in the
    cited paragraph — "anni" or "5" can no longer "prove" anything.
    """
    q = canon(quote)
    return len(q.split()) >= min_words and f" {q} " in f" {canon(source)} "
