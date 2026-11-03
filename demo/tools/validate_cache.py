"""V5 validation: semantic cache threshold sweep.

Computes every (document, clause) answer once, then replays the workload
through SemanticCache at several thresholds. For each threshold it reports
the hit rate and the false-positive rate - a similarity-served answer that
disagrees with the fresh computation. The plan's rule: a wrong cached answer
is worse than a miss, so the operating point is the lowest threshold whose
FP rate is zero (or the agreed tolerance).

    cd demo && uv run python tools/validate_cache.py --docs 12
"""
from __future__ import annotations

import argparse

from hybrid_demo.pipeline.review import load_documents
from hybrid_demo.specialist.cache import SemanticCache
from hybrid_demo.specialist.qa import best_spans, question_for

THRESHOLDS = (0.93, 0.95, 0.97, 0.98, 0.99)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/synthetic")
    parser.add_argument("--docs", type=int, default=12)
    parser.add_argument("--passes", type=int, default=2)
    args = parser.parse_args()

    import torch
    from transformers import AutoModelForQuestionAnswering, AutoTokenizer

    tok = AutoTokenizer.from_pretrained("data/cuad-models/roberta-large")
    model = AutoModelForQuestionAnswering.from_pretrained(
        "data/cuad-models/roberta-large", dtype=torch.float32)
    model.eval()

    docs, texts, clause_keys = load_documents(args.data, args.docs)
    clauses = [k.replace("_", " ") for k in clause_keys]
    unsupported = {c for c in clauses if question_for(c) is None}
    if unsupported:
        # CUAD has no data-protection category. Recording it as "not found"
        # would pollute the sweep with a field the specialist was never
        # asked, so skip it and report the omission.
        print(f"skipping clauses with no CUAD question: {', '.join(sorted(unsupported))}")
    supported = [c for c in clauses if c not in unsupported]
    requests = [(doc["doc_id"], clause) for doc in docs for clause in supported]

    print(f"fresh-computing {len(requests)} answers (one encoder pass each)...")
    fresh = {}
    for doc_id, clause in requests:
        question = question_for(clause)
        span = best_spans(model, tok, question, texts[doc_id])
        fresh[(doc_id, clause)] = {"clause": clause, "found": span is not None,
                                   "text": span.text if span else None}

    print(f"{'threshold':>9} {'hits':>12} {'similarity':>10} {'FP':>5} {'hit rate':>8} {'FP rate':>7}")
    for threshold in THRESHOLDS:
        cache = SemanticCache(threshold=threshold)
        hits = sim_hits = fp = 0
        for _ in range(args.passes):
            for doc_id, clause in requests:
                cached, served = cache.get(clause, texts[doc_id])
                if cached is None:
                    cache.put(clause, texts[doc_id], fresh[(doc_id, clause)])
                    continue
                hits += 1
                if served == "similarity":
                    sim_hits += 1
                    if (cached.get("found"), cached.get("text")) != \
                            (fresh[(doc_id, clause)]["found"], fresh[(doc_id, clause)]["text"]):
                        fp += 1
        total = len(requests) * args.passes
        sensitive = cache.stats.sensitive_skips
        print(f"{threshold:>9} {hits:>7}/{total:<4} {sim_hits:>10} {fp:>5} "
              f"{hits / total:>8.1%} {fp / max(sim_hits, 1):>7.1%}"
              f"   (sensitive skipped: {sensitive})")
    print("\nsensitivity gate: sensitive documents are never stored or served "
          "by the cache at all (exact or similarity); skips are counted above.")


if __name__ == "__main__":
    main()
