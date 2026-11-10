"""Retrieval evaluation against CUAD's expert annotations.

Answers the two questions that decide the product architecture:
  1. which embedding model for legal retrieval (leaderboard says one thing;
     our corpus says what holds on CUAD contracts)
  2. does retrieve-then-extract beat pure-generative extraction (the SOTA
     ladder's prediction, against our measured 0.71-0.80 F1 baseline)

Two modes:
  within (default) — for every (document, clause-category) pair in the
  held-out test split, issue the official CUAD question as a retrieval query
  over that document's chunks (what a retrieve-then-extract pipeline feeds
  the extractor). A hit at rank k means a retrieved chunk overlaps the
  expert-annotated answer span. Reports recall@k and MRR.

  cross — the portfolio query: embed the whole test split as one index and,
  per clause category, rank documents by their best-scoring chunk ("which
  contracts contain an audit-rights clause?"). Gold documents are those with
  an expert-annotated span for the category. Reports MRR (first gold
  document), success@k (at least one gold document in the top k) and
  macro-averaged document recall@10. This is the regime MTEB(Law) mostly
  measures, unlike the within-document task.

The query-side prompt is a controlled variable: "default" uses the model's
own generic web-search instruction (the original baseline), "clause" a
clause-finding instruction applied verbatim to every model, or any named
prompt the model ships (e.g. litil's LegalQuAD-query). Passage-side prompts
are always the model's own.

    cd demo && uv run --extra retrieval hybrid-retrieval-eval \
        --model litillabs/litil-embed-0.6b --query-prompt clause
    cd demo && uv run --extra retrieval hybrid-retrieval-eval \
        --model litillabs/litil-embed-0.6b --mode cross
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path

from . import metrics as ranking_metrics
from .docling_chunking import available as docling_available
from .docling_chunking import chunk_document_auto

QUESTION_RE = re.compile(r'related to "(.+?)" that should be reviewed')

CLAUSE_QUERY_PROMPT = (
    "Instruct: Given a clause category, retrieve the contract clauses "
    "that should be reviewed\nQuery:"
)


def load_test_split(cuad_zip: Path) -> list[dict]:
    with zipfile.ZipFile(cuad_zip) as zf:
        full = json.loads(zf.read("CUADv1.json"))["data"]
        test = json.loads(zf.read("test.json"))["data"]
    titles = {d["title"] for d in test}
    return [d for d in full if d["title"] in titles]


def load_queries(cuad_zip: Path) -> dict[str, str]:
    """Official question text per category, from any document's qas."""
    with zipfile.ZipFile(cuad_zip) as zf:
        full = json.loads(zf.read("CUADv1.json"))["data"]
    queries: dict[str, str] = {}
    for qa in full[0]["paragraphs"][0]["qas"]:
        m = QUESTION_RE.search(qa["question"])
        if m and m.group(1) not in queries:
            queries[m.group(1)] = qa["question"]
    return queries


def annotated_pairs(doc: dict) -> list[tuple[str, list[tuple[int, int]]]]:
    """(category, [(answer_start, answer_end), ...]) for this document."""
    pairs: list[tuple[str, list[tuple[int, int]]]] = []
    seen: set[str] = set()
    for qa in doc["paragraphs"][0]["qas"]:
        m = QUESTION_RE.search(qa["question"])
        if not m or m.group(1) in seen:
            continue
        spans = [(a["answer_start"], a["answer_start"] + len(a["text"]))
                 for a in qa.get("answers", [])
                 if a.get("text") and a.get("answer_start", -1) >= 0]
        if spans:
            pairs.append((m.group(1), spans))
            seen.add(m.group(1))
    return pairs


def _resolve_query_prompt(model, style: str) -> tuple[str | None, str | None, str]:
    """(prompt_name, prompt_text, description) for the query side."""
    prompts = getattr(model, "prompts", None) or {}
    if style == "default":
        return ("query" if "query" in prompts else None), None, "model 'query' prompt"
    if style == "clause":
        return None, CLAUSE_QUERY_PROMPT, "clause-finding instruction"
    if style in prompts:
        return style, None, f"model prompt '{style}'"
    known = ", ".join(sorted(prompts)) or "none"
    raise SystemExit(f"--query-prompt {style!r} unknown; model prompts: {known}")


def _encoder(model, prompts: dict, q_name: str | None, q_text: str | None):
    def encode(texts: list[str], kind: str):
        if kind == "query":
            if q_text is not None:
                return model.encode(texts, prompt=q_text,
                                    normalize_embeddings=True)
            return model.encode(texts, prompt_name=q_name,
                                normalize_embeddings=True)
        # models name the passage-side prompt "document" or "passage"
        name = next((k for k in ("passage", "document") if k in prompts), None)
        return model.encode(texts, prompt_name=name, normalize_embeddings=True)
    return encode


def _load_docs(cuad_zip: Path, docs_limit: int | None, max_chars: int) -> list[dict]:
    docs = load_test_split(cuad_zip)
    docs = [d for d in docs if 400 <= len(d["paragraphs"][0]["context"]) <= max_chars]
    return docs[:docs_limit] if docs_limit else docs


def evaluate(model_name: str, cuad_zip: Path, docs_limit: int, max_chars: int,
             top_k: int = 10, query_prompt: str = "default",
             chunk_backend: str = "builtin", tokenizer_model: str | None = None,
             metrics_backend: str = "auto") -> dict:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)
    prompts = getattr(model, "prompts", None) or {}
    q_name, q_text, q_desc = _resolve_query_prompt(model, query_prompt)
    encode = _encoder(model, prompts, q_name, q_text)

    docs = _load_docs(cuad_zip, docs_limit, max_chars)
    queries = load_queries(cuad_zip)

    chunk_hits = {k: 0 for k in range(1, top_k + 1)}
    mrr_sum = 0.0
    total = 0
    per_category: dict[str, dict] = {}
    per_pair_first_hit: dict[str, int | None] = {}
    qrels: dict[str, set[str]] = {}
    runs: dict[str, list[str]] = {}

    for doc in docs:
        context = doc["paragraphs"][0]["context"]
        chunks = chunk_document_auto(doc["title"], context, backend=chunk_backend,
                                     tokenizer_model=tokenizer_model)
        if not chunks:
            continue
        chunk_vecs = encode([c.text for c in chunks], "passage")
        max_k = min(top_k, len(chunks))

        for category, spans in annotated_pairs(doc):
            if category not in queries:
                continue
            query_vec = encode([queries[category]], "query")[0]
            sims = (chunk_vecs @ query_vec).tolist()
            order = sorted(range(len(chunks)), key=lambda i: -sims[i])

            query_id = f"{doc['title']}::{category}"
            qrels[query_id] = {str(i) for i, chunk in enumerate(chunks)
                               if any(chunk.overlaps(s, e) for s, e in spans)}
            runs[query_id] = [str(i) for i in order]
            total += 1
            rank_of_first_hit = None
            for k in range(1, max_k + 1):
                if any(chunks[order[k - 1]].overlaps(s, e) for s, e in spans):
                    rank_of_first_hit = k
                    break
            per_pair_first_hit[f"{doc['title']}::{category}"] = rank_of_first_hit
            if rank_of_first_hit:
                for k in range(rank_of_first_hit, top_k + 1):
                    chunk_hits[k] += 1
                mrr_sum += 1.0 / rank_of_first_hit

            stats = per_category.setdefault(category, {"total": 0, "hit5": 0})
            stats["total"] += 1
            if rank_of_first_hit and rank_of_first_hit <= 5:
                stats["hit5"] += 1

    ranking = ranking_metrics.evaluate(
        qrels, runs, metrics=("recall@1", "recall@5", "recall@10", "mrr",
                              "ndcg@10", "map@10"),
        backend=metrics_backend)
    used_chunk_backend = ("docling"
                          if chunk_backend == "docling"
                          or (chunk_backend == "auto" and docling_available())
                          else "builtin")
    result = {
        "mode": "within-doc",
        "model": model_name,
        "query_prompt": q_desc,
        "docs": len(docs),
        "pairs": total,
        "chunk_backend": used_chunk_backend,
        "recall_at_k": {f"@{k}": round(chunk_hits[k] / max(total, 1), 3)
                        for k in (1, 5, 10)},
        "mrr": round(mrr_sum / max(total, 1), 3),
        "ndcg_at_10": ranking["metrics"].get("ndcg@10"),
        "map_at_10": ranking["metrics"].get("map@10"),
        "ranking_backend": ranking["backend"],
        "per_pair_first_hit": per_pair_first_hit,
    }
    result["per_category_hit5"] = {
        cat: round(s["hit5"] / max(s["total"], 1), 3)
        for cat, s in sorted(per_category.items(), key=lambda x: -x[1]["total"])[:10]
    }
    return result


def evaluate_crossdoc(model_name: str, cuad_zip: Path, docs_limit: int,
                      max_chars: int, query_prompt: str = "default",
                      chunk_backend: str = "builtin",
                      tokenizer_model: str | None = None,
                      metrics_backend: str = "auto") -> dict:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)
    prompts = getattr(model, "prompts", None) or {}
    q_name, q_text, q_desc = _resolve_query_prompt(model, query_prompt)
    encode = _encoder(model, prompts, q_name, q_text)

    docs = _load_docs(cuad_zip, docs_limit, max_chars)
    queries = load_queries(cuad_zip)

    chunks = []
    chunk_doc: list[int] = []
    gold: dict[str, set[int]] = {}
    for i, doc in enumerate(docs):
        doc_chunks = chunk_document_auto(doc["title"], doc["paragraphs"][0]["context"],
                                         backend=chunk_backend,
                                         tokenizer_model=tokenizer_model)
        if not doc_chunks:
            continue
        for c in doc_chunks:
            chunks.append(c)
            chunk_doc.append(i)
        for category, _spans in annotated_pairs(doc):
            gold.setdefault(category, set()).add(i)
    chunk_vecs = encode([c.text for c in chunks], "passage")
    n_docs = len({d for d in chunk_doc})

    categories = sorted(c for c in gold if c in queries)
    mrr_sum = 0.0
    success = {1: 0, 10: 0}
    recall10_sum = 0.0
    per_category: dict[str, dict] = {}
    qrels: dict[str, set[str]] = {}
    runs: dict[str, list[str]] = {}

    for category in categories:
        query_vec = encode([queries[category]], "query")[0]
        sims = (chunk_vecs @ query_vec).tolist()
        order = sorted(range(len(chunks)), key=lambda i: -sims[i])

        best_chunk_rank: dict[int, int] = {}
        for rank, ci in enumerate(order, 1):
            d = chunk_doc[ci]
            if d not in best_chunk_rank:
                best_chunk_rank[d] = rank
                if len(best_chunk_rank) == n_docs:
                    break
        doc_rank = {d: r for r, d in
                    enumerate(sorted(best_chunk_rank,
                                     key=lambda d: best_chunk_rank[d]), 1)}
        gold_ranks = sorted(doc_rank[d] for d in gold[category])
        first = gold_ranks[0]
        qrels[category] = {str(d) for d in gold[category]}
        runs[category] = [str(d) for d in sorted(doc_rank, key=lambda d: doc_rank[d])]

        mrr_sum += 1.0 / first
        for k in success:
            if first <= k:
                success[k] += 1
        recall10_sum += sum(1 for r in gold_ranks if r <= 10) / len(gold_ranks)
        per_category[category] = {
            "gold_docs": len(gold_ranks),
            "first_gold_doc_rank": first,
        }

    n = max(len(categories), 1)
    ranking = ranking_metrics.evaluate(
        qrels, runs, metrics=("recall@10", "mrr", "ndcg@10", "map@10"),
        backend=metrics_backend)
    used_chunk_backend = ("docling"
                          if chunk_backend == "docling"
                          or (chunk_backend == "auto" and docling_available())
                          else "builtin")
    return {
        "mode": "cross-doc",
        "model": model_name,
        "query_prompt": q_desc,
        "docs": n_docs,
        "chunks": len(chunks),
        "categories": len(categories),
        "chunk_backend": used_chunk_backend,
        "mrr": round(mrr_sum / n, 3),
        "success_at_k": {f"@{k}": round(v / n, 3) for k, v in success.items()},
        "doc_recall_at_10": round(recall10_sum / n, 3),
        "ndcg_at_10": ranking["metrics"].get("ndcg@10"),
        "map_at_10": ranking["metrics"].get("map@10"),
        "ranking_backend": ranking["backend"],
        "per_category": per_category,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="CUAD retrieval evaluation")
    parser.add_argument("--model", required=True)
    parser.add_argument("--cuad", default="data/cuad/cuad-main/data.zip")
    parser.add_argument("--docs", type=int, default=None,
                        help="documents to use (within mode: default 20; "
                             "cross mode: default all)")
    parser.add_argument("--max-chars", type=int, default=24000)
    parser.add_argument("--mode", choices=("within", "cross"), default="within")
    parser.add_argument("--query-prompt", default="default",
                        help="'default' (the model's own query prompt), "
                             "'clause' (clause-finding instruction), or a "
                             "named model prompt (e.g. LegalQuAD-query)")
    parser.add_argument("--chunk-backend", default="builtin",
                        choices=("builtin", "docling", "auto"),
                        help="builtin keeps historical numbers; docling uses "
                             "HybridChunker (requires the 'ingest' extra)")
    parser.add_argument("--tokenizer-model", default=None,
                        help="tokenizer for docling HybridChunker (usually the "
                             "embedding model, e.g. litil-embed-0.6b)")
    parser.add_argument("--metrics-backend", default="auto",
                        choices=("auto", "ranx", "internal"))
    args = parser.parse_args()
    if args.chunk_backend == "docling" and not docling_available():
        raise SystemExit("docling is not installed; install the 'ingest' extra")

    if args.mode == "within":
        limit = args.docs if args.docs is not None else 20
        result = evaluate(args.model, Path(args.cuad), limit, args.max_chars,
                          query_prompt=args.query_prompt,
                          chunk_backend=args.chunk_backend,
                          tokenizer_model=args.tokenizer_model,
                          metrics_backend=args.metrics_backend)
    else:
        limit = args.docs if args.docs is not None else 0
        result = evaluate_crossdoc(args.model, Path(args.cuad), limit,
                                   args.max_chars,
                                   query_prompt=args.query_prompt,
                                   chunk_backend=args.chunk_backend,
                                   tokenizer_model=args.tokenizer_model,
                                   metrics_backend=args.metrics_backend)
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
