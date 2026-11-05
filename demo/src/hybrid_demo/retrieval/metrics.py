"""Ranking metrics for retrieval evaluation.

The evaluator already computed recall@k and MRR by hand.  This module keeps
those exact definitions (so historical numbers do not move) and adds NDCG and
MAP, delegating to ``ranx`` when it is installed.  ``backend`` records which
implementation produced the number, because a silent metric swap is exactly
the kind of change that should be visible in a measured report.

Inputs are plain dicts:

    qrels = {"q1": {"chunk-3", "chunk-7"}, ...}
    runs  = {"q1": ["chunk-3", "chunk-9", "chunk-7"], ...}

Only binary relevance is modelled, which is what CUAD's annotated spans give
us (a chunk either overlaps an answer span or it does not).
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


def _dedupe(ranked: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in ranked:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if k <= 0:
        return 0.0
    return len(set(ranked[:k]) & relevant) / k


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    for rank, item in enumerate(ranked, 1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def average_precision(ranked: Sequence[str], relevant: set[str],
                      k: int | None = None) -> float:
    if not relevant:
        return 0.0
    cutoff = min(len(ranked), k) if k else len(ranked)
    hits = 0
    total = 0.0
    for rank, item in enumerate(ranked[:cutoff], 1):
        if item in relevant:
            hits += 1
            total += hits / rank
    denominator = min(len(relevant), k) if k else len(relevant)
    return total / denominator if denominator else 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    dcg = 0.0
    for rank, item in enumerate(ranked[:k], 1):
        if item in relevant:
            dcg += 1.0 / math.log2(rank + 1)
    ideal = sum(1.0 / math.log2(rank + 1)
                for rank in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


def _metric_value(name: str, ranked: list[str], relevant: set[str]) -> float:
    metric, _, cutoff = name.partition("@")
    k = int(cutoff) if cutoff else len(ranked)
    metric = metric.lower()
    if metric in ("recall", "r"):
        return recall_at_k(ranked, relevant, k)
    if metric in ("precision", "p"):
        return precision_at_k(ranked, relevant, k)
    if metric in ("mrr", "rr"):
        return reciprocal_rank(ranked[:k] if cutoff else ranked, relevant)
    if metric in ("ndcg", "ndcg_cut"):
        return ndcg_at_k(ranked, relevant, k)
    if metric in ("map", "ap"):
        return average_precision(ranked, relevant, k if cutoff else None)
    raise ValueError(f"unknown metric {name!r}")


def evaluate(qrels: dict[str, set[str]], runs: dict[str, list[str]],
             metrics: Sequence[str] = ("recall@1", "recall@5", "recall@10",
                                       "mrr", "ndcg@10", "map@10"),
             backend: str = "auto") -> dict:
    """Evaluate ranked lists, returning ``{metric: mean_score, ...}``."""
    if backend not in ("auto", "ranx", "internal"):
        raise ValueError(f"unknown backend {backend!r}")
    query_ids = sorted(set(qrels) & set(runs))
    if not query_ids:
        return {"backend": "internal", "queries": 0,
                "metrics": {name: 0.0 for name in metrics}}
    if backend in ("auto", "ranx"):
        try:
            from ranx import Qrels, Run
            from ranx import evaluate as ranx_evaluate

            qrels_obj = Qrels({qid: {doc: 1 for doc in qrels[qid]} for qid in query_ids})
            run_obj = Run({qid: {doc: 1.0 / (rank + 1)
                                 for rank, doc in enumerate(runs[qid])}
                           for qid in query_ids})
            scores = ranx_evaluate(qrels_obj, run_obj, list(metrics))
            return {"backend": "ranx",
                    "queries": len(query_ids),
                    "metrics": {name: round(float(scores[name]), 4) for name in metrics}}
        except ImportError:
            if backend == "ranx":
                raise
    values: dict[str, float] = {}
    for name in metrics:
        total = 0.0
        for qid in query_ids:
            total += _metric_value(name, _dedupe(runs[qid]), qrels[qid])
        values[name] = round(total / max(len(query_ids), 1), 4)
    return {"backend": "internal", "queries": len(query_ids), "metrics": values}
