"""PDF extraction and deterministic, page-aware token chunking."""

import re
from dataclasses import dataclass

import pymupdf
import tiktoken

ENCODING = tiktoken.get_encoding("cl100k_base")


@dataclass(frozen=True)
class Page:
    number: int
    content: str


@dataclass(frozen=True)
class Chunk:
    index: int
    page_number: int
    content: str
    token_count: int


def normalize_text(text: str) -> str:
    """Collapse extraction whitespace for readable, searchable snippets."""
    return re.sub(r"\s+", " ", text).strip()


def parse_pdf(data: bytes) -> list[Page]:
    with pymupdf.open(stream=data, filetype="pdf") as document:
        return [Page(number=i + 1, content=normalize_text(page.get_text())) for i, page in enumerate(document)]


def count_tokens(text: str) -> int:
    return len(ENCODING.encode(text))


def _render(units: list[tuple[str, bool]]) -> str:
    """Join word pieces, preserving spaces between words only."""
    return "".join((" " if spaced and i else "") + text for i, (text, spaced) in enumerate(units))


def _split_long_word(word: str, size: int) -> list[str]:
    """Split an unusually long token sequence on Unicode character boundaries."""
    parts: list[str] = []
    while word:
        low, high = 1, len(word)
        while low < high:
            middle = (low + high + 1) // 2
            if count_tokens(word[:middle]) <= size:
                low = middle
            else:
                high = middle - 1
        while low > 1 and count_tokens(word[:low]) > size:
            low -= 1
        if count_tokens(word[:low]) > size:
            raise ValueError("Chunk size is too small for one character")
        parts.append(word[:low])
        word = word[low:]
    return parts


def chunk_pages(pages: list[Page], size: int = 500, overlap: int = 75) -> list[Chunk]:
    if size < 1 or overlap < 0 or overlap >= size:
        raise ValueError("Chunk size must be positive and overlap must be smaller")

    result: list[Chunk] = []
    for page in pages:
        # The input is normalized: words separated by single spaces.
        units: list[tuple[str, bool]] = []
        for word in page.content.split(" "):
            for i, part in enumerate(_split_long_word(word, size)):
                units.append((part, i == 0))
        if not units:
            continue

        start = 0
        while start < len(units):
            end = start + 1
            while end < len(units) and count_tokens(_render(units[start : end + 1])) <= size:
                end += 1

            content = _render(units[start:end])
            result.append(Chunk(len(result), page.number, content, count_tokens(content)))
            if end == len(units):
                break

            next_start = end
            if overlap:
                for candidate in range(end - 1, start, -1):
                    if count_tokens(_render(units[candidate:end])) > overlap:
                        break
                    next_start = candidate
            start = next_start

    return result
