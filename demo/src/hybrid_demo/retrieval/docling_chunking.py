"""docling ingestion/chunking adapter.

``chunking.py`` is a good structure-aware chunker, but the product direction
(§11) already commits to docling for PDF/DOCX ingestion.  Duplicating the
chunking step would create two boundary definitions and two offset bugs.

This adapter feeds docling's ``HybridChunker`` (tokenizer-aware, hierarchy-
preserving, table headers repeated) and maps its provenance back to character
offsets.  Two caveats are deliberate:

* Offsets are relative to the *exported text* (``document.export_to_text()``),
  which is what the pipeline embeds and quotes.  Mapping to PDF page coordinates
  is a separate problem (and not what CUAD's annotations use).
* If a provenance charspan cannot be trusted, the adapter re-locates the
  chunk text in the exported text and counts it in the returned ``relocated``
  metadata. Silently using a wrong offset is the D11 failure mode; relocation
  is visible.
* A raw string is wrapped as Markdown (``<doc_id>.md``) because that is the
  format docling accepts without a page layout; PDF/DOCX paths are passed
  through untouched.

The default remains the built-in chunker, so recorded retrieval numbers do not
move until ``--chunk-backend docling`` is measured.
"""
from __future__ import annotations

import importlib.util
from io import BytesIO
from pathlib import Path
from typing import Any

from ..common import normalize_text
from .chunking import Chunk, chunk_document


def available() -> bool:
    return (importlib.util.find_spec("docling") is not None
            and importlib.util.find_spec("docling_core") is not None)


def _tokenizer(model_name: str | None, max_tokens: int):
    if not model_name:
        return None
    try:
        from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
        from transformers import AutoTokenizer
    except ImportError:
        return None
    return HuggingFaceTokenizer(tokenizer=AutoTokenizer.from_pretrained(model_name),
                                max_tokens=max_tokens)


def _locate(text: str, needle: str, start: int = 0) -> tuple[int, int] | None:
    """Same relocation rule as evalkit.spans.locate (kept local to avoid a
    retrieval -> evalkit dependency)."""
    if not text or not needle:
        return None
    idx = text.find(needle, start)
    if idx >= 0:
        return idx, idx + len(needle)
    import re

    compact_chars: list[str] = []
    positions: list[int] = []
    in_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not in_space and compact_chars:
                compact_chars.append(" ")
                positions.append(i)
            in_space = True
            continue
        in_space = False
        compact_chars.append(ch.lower())
        positions.append(i)
    compact = "".join(compact_chars)
    needle_compact = re.sub(r"\s+", " ", needle.strip()).lower()
    idx = compact.find(needle_compact)
    if idx < 0:
        return None
    end = idx + len(needle_compact)
    return positions[idx], positions[min(end, len(positions) - 1)] + 1


def chunk_with_docling(doc_id: str, source: str | Path | bytes,
                       max_tokens: int = 400, tokenizer_model: str | None = None,
                       merge_peers: bool = True,
                       ) -> tuple[list[Chunk], str, dict[str, Any]]:
    """Convert and chunk one document; returns (chunks, exported_text, meta).

    Raises ``RuntimeError`` with an install hint when docling is absent.
    """
    if not available():
        raise RuntimeError(
            "docling is not installed; install the 'ingest' extra "
            "(uv sync --extra ingest) or use --chunk-backend builtin"
        )
    from docling.chunking import HybridChunker
    from docling.datamodel.base_models import DocumentStream
    from docling.document_converter import DocumentConverter

    if isinstance(source, bytes):
        stream = DocumentStream(name=f"{doc_id}.md", stream=BytesIO(source))
    elif isinstance(source, Path):
        stream = source
    elif isinstance(source, str) and "\n" not in source and len(source) < 4096:
        try:
            candidate = Path(source)
            stream = candidate if candidate.exists() else DocumentStream(
                name=f"{doc_id}.md", stream=BytesIO(source.encode()))
        except OSError:
            stream = DocumentStream(name=f"{doc_id}.md", stream=BytesIO(source.encode()))
    else:
        stream = DocumentStream(name=f"{doc_id}.md", stream=BytesIO(str(source).encode()))

    converter = DocumentConverter()
    document = converter.convert(stream).document
    text = document.export_to_text()
    chunker = HybridChunker(tokenizer=_tokenizer(tokenizer_model, max_tokens),
                            max_tokens=max_tokens, merge_peers=merge_peers)
    chunks: list[Chunk] = []
    relocated = 0
    cursor = 0
    for item in chunker.chunk(dl_doc=document):
        body = item.text
        span = None
        for doc_item in getattr(item.meta, "doc_items", []) or []:
            for prov in getattr(doc_item, "prov", []) or []:
                charspan = getattr(prov, "charspan", None)
                if charspan:
                    span = (int(charspan[0]), int(charspan[1]))
                    break
            if span:
                break
        if span is not None and span[1] > span[0]:
            # The provenance charspan can address only the first item of a
            # merged chunk, or a different export than the one we quote.  If
            # the slice does not corroborate the chunk text, relocate instead
            # of trusting an offset that would fail the D11 regression class.
            candidate = normalize_text(text[span[0]:span[1]])
            wanted = normalize_text(body)
            if wanted and candidate and wanted not in candidate and candidate not in wanted:
                relocated_span = _locate(text, body, cursor)
                if relocated_span is not None:
                    span = relocated_span
                    relocated += 1
        if span is None or span[1] <= span[0]:
            span = _locate(text, body, cursor)
            relocated += int(span is not None)
        if span is None:
            # Last resort: keep the chunk but anchor it at the cursor.  The
            # metadata says so, so a retrieval hit can be excluded from
            # evidence rather than quoted with a false offset.
            span = (cursor, min(len(text), cursor + len(body)))
            relocated += 1
        cursor = max(cursor, span[1])
        chunks.append(Chunk(doc_id, body, span[0], span[1]))
    return chunks, text, {"converter": "docling", "chunker": "HybridChunker",
                          "chunks": len(chunks), "relocated": relocated,
                          "max_tokens": max_tokens, "merge_peers": merge_peers}


def chunk_document_auto(doc_id: str, text: str, max_tokens: int = 400,
                        min_tokens: int = 60, backend: str = "builtin",
                        tokenizer_model: str | None = None) -> list[Chunk]:
    """Choose the built-in or docling chunker.

    ``backend='auto'`` uses docling when installed, otherwise builtin.  The
    built-in path is unchanged, including ``min_tokens``, so historical
    numbers stay reproducible.
    """
    if backend not in ("builtin", "docling", "auto"):
        raise ValueError(f"unknown chunk backend {backend!r}")
    if backend == "builtin" or (backend == "auto" and not available()):
        return chunk_document(doc_id, text, max_tokens=max_tokens, min_tokens=min_tokens)
    chunks, _text, _meta = chunk_with_docling(doc_id, text, max_tokens=max_tokens,
                                              tokenizer_model=tokenizer_model)
    return chunks
