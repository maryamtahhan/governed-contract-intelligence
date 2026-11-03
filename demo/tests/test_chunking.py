"""Chunking properties the retrieval eval's hit definition depends on:
chunks tile the document (a gold span inside any paragraph must overlap
some chunk), carry verbatim text, and stay within bounds.

The oversized-paragraph path once sliced the *document* with
paragraph-local sentence offsets, producing chunks with text from the
document's start and ranges that restart at 0 — gold spans deep in the
document overlapped no chunk. These tests pin the fix.
"""
from hybrid_demo.retrieval.chunking import _split_paragraphs, chunk_document


def _small():
    return "First paragraph with some text. Second sentence here."


def _oversized():
    # > 400 est tokens: forces the sentence-split path
    return "Oversized legal paragraph. " * 120


def test_chunks_tile_paragraphs_with_oversized_paragraph():
    text = f"{_small()}\n\n{_oversized()}\n\nFinal small paragraph."
    chunks = chunk_document("doc", text)
    assert len(chunks) > 1
    for start, end in _split_paragraphs(text):
        covered = sum(min(c.char_end, end) - max(c.char_start, start)
                      for c in chunks
                      if c.char_start < end and start < c.char_end)
        assert covered >= (end - start) - 8, (start, end, covered)


def test_chunk_ranges_in_bounds_and_monotonic():
    text = f"{_small()}\n\n{_oversized()}\n\nFinal small paragraph."
    chunks = chunk_document("doc", text)
    assert all(c.char_start <= c.char_end <= len(text) for c in chunks)
    starts = [c.char_start for c in chunks]
    assert starts == sorted(starts)
    assert starts[0] == 0 or text[:starts[0]].strip() == ""


def test_chunk_text_is_verbatim_document_text():
    import re

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s)

    text = f"{_small()}\n\n{_oversized()}\n\nFinal small paragraph."
    chunks = chunk_document("doc", text)
    flat = norm(text)
    for c in chunks:
        assert c.text
        assert norm(c.text)[:120] in flat


def test_offsets_survive_annotation_overlap():
    """A span placed inside the oversized paragraph must overlap a chunk."""
    big = _oversized()
    text = f"{_small()}\n\n{big}\n\nFinal small paragraph."
    span_start = len(_small()) + 2 + 50
    span_end = span_start + 120
    chunks = chunk_document("doc", text)
    assert any(c.char_start < span_end and span_start < c.char_end
               for c in chunks)


def test_small_document_single_chunk():
    text = "One short paragraph."
    chunks = chunk_document("doc", text)
    assert len(chunks) == 1
    assert chunks[0].text == text


def test_unpunctuated_sentence_is_hard_split_to_budget():
    from hybrid_demo.common import est_tokens

    text = "prefix " + "x" * 5000 + " suffix"
    chunks = chunk_document("doc", text, max_tokens=100)
    assert len(chunks) > 1
    assert all(est_tokens(c.text) <= 100 for c in chunks)
    covered = " ".join(c.text for c in chunks)
    assert covered.replace(" ", "") == text.replace(" ", "")
    assert all(c.char_start <= c.char_end <= len(text) for c in chunks)
