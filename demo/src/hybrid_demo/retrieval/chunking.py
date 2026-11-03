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
                # sentence offsets are paragraph-local: slice para, and
                # translate to global offsets for the chunk range
                if current and est_tokens(current) + est_tokens(para[sent_start:sent_end]) > max_tokens:
                    flush()
                if current_start is None:
                    current_start = start + sent_start
                current += para[sent_start:sent_end]
        else:
            if current_start is None:
                current_start = start
            current += ("\n" if current else "") + para
        if est_tokens(current) >= min_tokens and est_tokens(current) >= max_tokens * 0.8:
            flush()
    flush()
    return chunks


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
