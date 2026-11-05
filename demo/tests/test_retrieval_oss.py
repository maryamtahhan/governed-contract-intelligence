from __future__ import annotations

import pytest

from hybrid_demo.retrieval import metrics
from hybrid_demo.retrieval.chunking import Chunk, chunk_document
from hybrid_demo.retrieval.docling_chunking import available as docling_available
from hybrid_demo.retrieval.docling_chunking import chunk_document_auto
from hybrid_demo.retrieval.index import PortfolioIndex, build_index


def test_ranking_metrics_known_example():
    qrels = {"q1": {"a", "b"}}
    runs = {"q1": ["a", "c", "b"]}
    result = metrics.evaluate(qrels, runs, metrics=(
        "recall@1", "recall@3", "mrr", "ndcg@3", "map@3"), backend="internal")
    values = result["metrics"]
    assert values["recall@1"] == 0.5
    assert values["recall@3"] == 1.0
    assert values["mrr"] == 1.0
    assert values["ndcg@3"] == pytest.approx(0.9197, abs=1e-3)
    assert values["map@3"] == pytest.approx(0.8333, abs=1e-3)
    assert result["backend"] == "internal"


def test_ranx_backend_requires_install():
    import importlib.util

    if importlib.util.find_spec("ranx"):
        pytest.skip("ranx installed")
    with pytest.raises(ImportError):
        metrics.evaluate({"q": {"a"}}, {"q": ["a"]}, backend="ranx")


def _fake_embed(text: str) -> list[float]:
    return [float(len(text) % 7), float(text.count("law") + 1), float(text.count("pay") + 1)]


def test_memory_index_dense_and_hybrid():
    chunks = [
        Chunk("d1", "governing law is New York", 0, 28),
        Chunk("d2", "payment terms are net thirty", 29, 58),
        Chunk("d3", "the parties may terminate for convenience", 59, 105),
    ]
    index = build_index(chunks, _fake_embed, backend="memory")
    dense = index.search(index.vectors[0], k=2)
    assert dense[0].doc_id == "d1"
    hybrid = index.search_text("payment law", _fake_embed, k=3, alpha=0.5)
    assert len(hybrid) == 3
    assert {hit.source for hit in hybrid} == {"hybrid"}
    assert index.sparse_backend in {"bm25s", "internal"}


def test_index_roundtrip(tmp_path):
    chunks = [Chunk("d1", "governing law", 0, 12)]
    index = build_index(chunks, _fake_embed, path=tmp_path, backend="memory")
    loaded = PortfolioIndex.load(tmp_path)
    assert len(loaded.chunks) == 1
    assert loaded.vectors == index.vectors
    assert loaded.search(loaded.vectors[0], k=1)[0].text == "governing law"


def test_builtin_chunk_backend_is_unchanged():
    text = "First paragraph.\n\nSecond paragraph with more words."
    assert chunk_document_auto("d1", text, backend="builtin") == chunk_document("d1", text)


def test_docling_backend_is_explicit_when_missing():
    if docling_available():
        pytest.skip("docling installed")
    with pytest.raises(RuntimeError):
        chunk_document_auto("d1", "text", backend="docling")
    assert chunk_document_auto("d1", "text", backend="auto") == chunk_document("d1", "text")
