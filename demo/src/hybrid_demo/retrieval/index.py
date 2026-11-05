"""A small portfolio index: dense + sparse retrieval, persisted to disk.

The demo had no index at all: ``evaluate.py`` loaded documents and computed
cosine similarity in memory, and the product direction (§11) needs a
cross-document portfolio index.  Rather than build a vector database, this
module is a thin adapter with two backends:

* ``memory`` - pure Python, enough for the demo corpus and CI.
* ``lancedb`` - the embedded columnar vector store, used when installed.

Sparse retrieval uses ``bm25s`` when installed and a compact internal BM25
otherwise.  Hybrid search combines min-max-normalised dense and sparse
scores, so the same code covers both measured regimes (within-doc
retrieve-then-extract and cross-doc portfolio queries) without inventing a
second API.

The index keeps chunk text and character offsets alongside vectors, because
the whole point of the pipeline is evidence: a hit must be quotable and
checkable against annotation spans.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .chunking import Chunk

_TOKEN = re.compile(r"[a-z0-9]+")
_FILES = {"chunks": "chunks.jsonl", "vectors": "vectors.json", "manifest": "manifest.json"}


@dataclass
class Hit:
    doc_id: str
    text: str
    char_start: int
    char_end: int
    score: float
    source: str

    def as_dict(self) -> dict:
        return asdict(self)


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


class _BM25:
    """Okapi BM25 over the in-memory chunk texts (k1=1.5, b=0.75).

    ``bm25s`` is used when installed (it is a fast eager-sparse implementation);
    the compact internal scorer is the fallback.  The fallback is not an error:
    the ranking contract is the same, only the arithmetic/implementation is
    different, and the backend is reported by :class:`PortfolioIndex`.
    """

    def __init__(self, documents: Sequence[str]) -> None:
        self.raw = list(documents)
        self.documents = [_tokens(text) for text in documents]
        self.doc_freq: Counter[str] = Counter()
        for tokens in self.documents:
            self.doc_freq.update(set(tokens))
        self.avg_len = (sum(len(t) for t in self.documents) / len(self.documents)
                        if self.documents else 0.0)
        self._external = None
        try:
            import bm25s

            corpus_tokens = bm25s.tokenize(self.raw)
            retriever = bm25s.BM25()
            retriever.index(corpus_tokens)
            self._external = (bm25s, retriever)
        except Exception:  # noqa: BLE001 - optional backend/version mismatch
            self._external = None

    @property
    def backend(self) -> str:
        return "bm25s" if self._external is not None else "internal"

    def scores(self, query: str, k1: float = 1.5, b: float = 0.75) -> list[float]:
        if self._external is not None:
            try:
                bm25s, retriever = self._external
                query_tokens = bm25s.tokenize([query])
                results, scores = retriever.retrieve(
                    query_tokens, k=len(self.documents))
                out = [0.0] * len(self.documents)
                for doc_id, score in zip(results[0], scores[0], strict=False):
                    out[int(doc_id)] = float(score)
                return out
            except Exception:  # noqa: BLE001 - fall through to the internal scorer
                self._external = None
        query_tokens = _tokens(query)
        total = len(self.documents)
        out: list[float] = []
        for tokens in self.documents:
            counts = Counter(tokens)
            length = len(tokens)
            score = 0.0
            for token in query_tokens:
                if token not in counts:
                    continue
                df = self.doc_freq.get(token, 0)
                idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
                numerator = counts[token] * (k1 + 1)
                denominator = counts[token] + k1 * (1 - b + b * length / (self.avg_len or 1))
                score += idf * numerator / denominator
            out.append(score)
        return out


class PortfolioIndex:
    def __init__(self, backend: str = "memory", path: str | Path | None = None) -> None:
        if backend not in ("memory", "lancedb", "auto"):
            raise ValueError(f"unknown index backend {backend!r}")
        self.requested_backend = backend
        self.backend = "memory"
        self.path = Path(path) if path else None
        self.chunks: list[Chunk] = []
        self.vectors: list[list[float]] = []
        self._bm25: _BM25 | None = None
        self._lance = None
        if backend in ("lancedb", "auto") and path:
            self._try_lancedb()

    def _try_lancedb(self) -> None:
        try:
            import lancedb  # noqa: F401
        except ImportError:
            if self.requested_backend == "lancedb":
                raise RuntimeError(
                    "lancedb is not installed; install the 'ingest' extra"
                ) from None
            return
        self.backend = "lancedb"

    def add(self, chunks: Iterable[Chunk], vectors: Sequence[Sequence[float]] | None = None,
            embed_fn: Callable[[str], Sequence[float]] | None = None) -> None:
        new_chunks = list(chunks)
        if vectors is None:
            if embed_fn is None:
                raise ValueError("provide vectors or embed_fn")
            vectors = [embed_fn(chunk.text) for chunk in new_chunks]
        if len(new_chunks) != len(vectors):
            raise ValueError("chunks and vectors differ in length")
        self.chunks.extend(new_chunks)
        self.vectors.extend([list(v) for v in vectors])
        self._bm25 = None
        if self.backend == "lancedb" and self.path:
            self._write_lance()

    def _write_lance(self) -> None:
        import lancedb

        database = lancedb.connect(str(self.path))
        rows = [{"doc_id": c.doc_id, "text": c.text, "char_start": c.char_start,
                 "char_end": c.char_end, "vector": list(v)}
                for c, v in zip(self.chunks, self.vectors, strict=False)]
        if not rows:
            return
        if "chunks" in database.table_names():
            database.drop_table("chunks")
        database.create_table("chunks", data=rows)

    def _dense(self, vector: Sequence[float]) -> list[float]:
        if not self.vectors:
            return []
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        out = []
        for candidate in self.vectors:
            cnorm = math.sqrt(sum(x * x for x in candidate)) or 1.0
            out.append(sum(a * b for a, b in zip(vector, candidate, strict=False)) / (norm * cnorm))
        return out

    def search(self, vector: Sequence[float], k: int = 5) -> list[Hit]:
        scores = self._dense(vector)
        order = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
        return [Hit(self.chunks[i].doc_id, self.chunks[i].text,
                    self.chunks[i].char_start, self.chunks[i].char_end,
                    round(scores[i], 6), "dense") for i in order]

    @property
    def sparse_backend(self) -> str:
        return self._bm25.backend if self._bm25 is not None else "internal"

    def _sparse(self, query: str) -> list[float]:
        if self._bm25 is None:
            self._bm25 = _BM25([c.text for c in self.chunks])
        return self._bm25.scores(query)

    def search_text(self, query: str, embed_fn: Callable[[str], Sequence[float]],
                    k: int = 5, alpha: float = 0.5) -> list[Hit]:
        """Hybrid search: ``alpha`` weights dense, ``1-alpha`` weights BM25."""
        if not self.chunks:
            return []
        dense = self._dense(embed_fn(query))
        sparse = self._sparse(query)

        def normalize(values: list[float]) -> list[float]:
            if not values:
                return values
            high, low = max(values), min(values)
            return [0.0] * len(values) if high == low else [(v - low) / (high - low) for v in values]

        dense_n, sparse_n = normalize(dense), normalize(sparse)
        combined = [alpha * d + (1 - alpha) * s for d, s in zip(dense_n, sparse_n, strict=False)]
        order = sorted(range(len(combined)), key=lambda i: -combined[i])[:k]
        return [Hit(self.chunks[i].doc_id, self.chunks[i].text,
                    self.chunks[i].char_start, self.chunks[i].char_end,
                    round(combined[i], 6), "hybrid") for i in order]

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("no path to save to")
        target.mkdir(parents=True, exist_ok=True)
        with (target / _FILES["chunks"]).open("w", encoding="utf-8") as handle:
            for chunk in self.chunks:
                handle.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")
        (target / _FILES["vectors"]).write_text(json.dumps(self.vectors))
        (target / _FILES["manifest"]).write_text(json.dumps({
            "backend": self.backend, "chunks": len(self.chunks),
            "dim": len(self.vectors[0]) if self.vectors else 0,
        }, indent=2))
        return target

    @classmethod
    def load(cls, path: str | Path, backend: str = "memory") -> PortfolioIndex:
        target = Path(path)
        chunks = [Chunk(**json.loads(line)) for line in
                  (target / _FILES["chunks"]).read_text().splitlines() if line.strip()]
        vectors = json.loads((target / _FILES["vectors"]).read_text())
        index = cls(backend=backend, path=target)
        index.chunks, index.vectors = chunks, vectors
        return index


def build_index(chunks: Iterable[Chunk], embed_fn: Callable[[str], Sequence[float]],
                path: str | Path | None = None, backend: str = "auto") -> PortfolioIndex:
    index = PortfolioIndex(backend=backend, path=path)
    index.add(chunks, embed_fn=embed_fn)
    if path:
        index.save(path)
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description="build/query a portfolio index")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--chunks", required=True, help="JSONL of chunk records")
    build.add_argument("--out", required=True)
    build.add_argument("--model", required=True)
    build.add_argument("--backend", default="auto", choices=("auto", "memory", "lancedb"))
    query = sub.add_parser("query")
    query.add_argument("--index", required=True)
    query.add_argument("--q", required=True)
    query.add_argument("--k", type=int, default=5)
    query.add_argument("--alpha", type=float, default=0.5)
    query.add_argument("--model", required=True)
    args = parser.parse_args()

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(args.model)
    embed_fn = lambda text: model.encode(text, normalize_embeddings=True).tolist()  # noqa: E731
    if args.command == "build":
        chunks = [Chunk(**json.loads(line)) for line in
                  Path(args.chunks).read_text().splitlines() if line.strip()]
        index = build_index(chunks, embed_fn, path=args.out, backend=args.backend)
        print(json.dumps({"path": args.out, "chunks": len(index.chunks),
                          "backend": index.backend}, indent=2))
        return
    index = PortfolioIndex.load(args.index)
    hits = index.search_text(args.q, embed_fn, k=args.k, alpha=args.alpha)
    print(json.dumps([hit.as_dict() for hit in hits], indent=2))


if __name__ == "__main__":
    main()
