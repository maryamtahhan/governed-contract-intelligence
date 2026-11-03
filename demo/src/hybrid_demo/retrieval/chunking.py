"""Structure-aware chunking that preserves character offsets.

Docling-style ingestion needs chunks that (a) respect document structure
(paragraphs, numbered sections), (b) fit an embedding model's window, and
(c) know their character range in the source, so retrieval hits can be
checked against annotation spans and quoted as evidence.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..common import est_tokens

PARAGRAPH_SEP = "\n\n"


@dataclass
class Chunk:
    doc_id: str
    text: str
    char_start: int
    char_end: int

    def overlaps(self, start: int, end: int) -> bool:
        return self.char_start < end and start < self.char_end


def _split_paragraphs(text: str) -> list[tuple[int, int]]:
    spans = []
    pos = 0
    for para in text.split(PARAGRAPH_SEP):
        stripped = para.strip()
        if stripped:
            offset = text.find(para, pos)
            spans.append((offset, offset + len(para)))
        pos += len(para) + len(PARAGRAPH_SEP)
    return spans


def chunk_document(doc_id: str, text: str, max_tokens: int = 400,
                   min_tokens: int = 60) -> list[Chunk]:
    """Group paragraphs into chunks within the token budget.

    Oversized single paragraphs are hard-split on sentence boundaries so no
    chunk exceeds the embedding window.
    """
    chunks: list[Chunk] = []
    current = ""
    current_start = None

    def flush() -> None:
        nonlocal current, current_start
        if current.strip() and current_start is not None:
            chunks.append(Chunk(doc_id, current.strip(), current_start,
                                current_start + len(current)))
        current, current_start = "", None

    for start, end in _split_paragraphs(text):
        para = text[start:end]
        if est_tokens(current) + est_tokens(para) > max_tokens and current:
            flush()
        if est_tokens(para) > max_tokens:
            flush()
            sentences = _split_sentences(para)
            for sent_start, sent_end in sentences:
                # A single sentence can still exceed the budget (no
                # punctuation for thousands of characters). Hard-split it so
                # no chunk is larger than the embedding window.
                for piece_start, piece_end in _fit_windows(
                        para[sent_start:sent_end], max_tokens):
                    piece = para[sent_start:sent_end][piece_start:piece_end]
                    if current and est_tokens(current) + est_tokens(piece) > max_tokens:
                        flush()
                    if current_start is None:
                        current_start = start + sent_start + piece_start
                    current += piece
                    if est_tokens(current) >= max_tokens:
                        flush()
        else:
            if current_start is None:
                current_start = start
            current += ("\n" if current else "") + para
        if est_tokens(current) >= min_tokens and est_tokens(current) >= max_tokens * 0.8:
            flush()
    flush()
    return chunks


def _fit_windows(text: str, max_tokens: int) -> list[tuple[int, int]]:
    """Split ``text`` into local spans that each fit ``max_tokens``.

    Prefers a whitespace boundary so words are not cut; falls back to a hard
    character cut when a single token-like run is longer than the budget.
    """
    if est_tokens(text) <= max_tokens:
        return [(0, len(text))]
    max_chars = max(1, max_tokens * 4)
    spans: list[tuple[int, int]] = []
    pos = 0
    while pos < len(text):
        end = min(len(text), pos + max_chars)
        if end < len(text):
            boundary = text.rfind(" ", pos + 1, end)
            if boundary > pos:
                end = boundary + 1
        spans.append((pos, end))
        pos = end
    return spans


def _split_sentences(para: str) -> list[tuple[int, int]]:
    spans = []
    pos = 0
    import re

    for m in re.finditer(r"[.;]\s+", para):
        end = m.end()
        if para[pos:end].strip():
            spans.append((pos, end))
        pos = end
    if para[pos:].strip():
        spans.append((pos, len(para)))
    return spans if spans else [(0, len(para))]
