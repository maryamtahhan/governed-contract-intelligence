"""Offline guardrail for the termination-for-cause trigger.

The CUAD specialist answers the canonical *Termination For Convenience*
question first.  When that question finds nothing, an optional follow-up asks
the *Termination For Cause* question.  The follow-up is gated by a trigger;
the measured default is the narrow regex in ``specialist.questions``.

This benchmark compares that regex with the opt-in Jev decision trigger on
the CUAD test split.  The adoption gate is **termination F1 >= 0.714** (the
regex baseline on this split).  It is an offline, local-CPU measurement of
the specialist weights: it does not exercise Praxis or a served pool, and it
cannot support a latency or cost claim.

Usage::

    cd demo
    uv run hybrid-cause-bench --docs 20
    uv run hybrid-cause-bench --deciders regex jev --thresholds 0.4 0.5 0.6
    uv run hybrid-cause-bench --json /tmp/cause.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..common import normalize_text
from ..specialist.questions import (
    CAUSE_TERMINATION_QUESTION,
    cause_jev_score,
    cause_language_present,
)

DEFAULT_THRESHOLDS = (0.4, 0.5, 0.6)
GUARDRAIL_F1 = 0.714


def text_matches(predicted: str | None, truth: str | None) -> bool:
    """The pipeline's containment match, unchanged."""
    if not predicted or not truth:
        return False
    p, t = normalize_text(predicted), normalize_text(truth)
    return p == t or p in t or t in p


def load_cuad_docs(data_dir: str | Path, docs: int | None = None) -> list[dict]:
    """Load the CUAD test corpus used by the recorded specialist run.

    ``data/cuad-test`` is the 20-file subset (19 unique titles) behind the
    0.714 baseline.  Using the same files and gold annotations is what makes
    the guardrail comparison valid; the full CUAD zip has a different,
    larger sample.  Duplicate titles are deduplicated in file order.
    """
    root = Path(data_dir)
    ground = json.loads((root / "ground_truth.json").read_text())
    result: list[dict] = []
    seen: set[str] = set()
    for doc in ground.get("documents") or []:
        doc_id = doc["doc_id"]
        if doc_id in seen:
            continue
        seen.add(doc_id)
        context = (root / "documents" / f"{doc_id}.txt").read_text()
        truth = (doc.get("clauses") or {}).get("termination") or None
        result.append({"doc_id": doc_id, "context": context, "truth": truth})
        if docs is not None and len(result) >= docs:
            break
    return result


def score_rows(rows: list[tuple[str | None, str | None]]) -> dict:
    """TP/FP/FN for ``(truth, prediction)`` pairs, matching the validator.

    The existing CUAD validator counts a wrong span as a false positive and
    does not additionally count it as a false negative.  Keeping the same
    rule makes the 0.714 guardrail comparable to the recorded baseline.
    """
    tp = fp = fn = 0
    for want, got in rows:
        if want is None:
            if got is not None:
                fp += 1
        elif got is not None and text_matches(got, want):
            tp += 1
        elif got is not None:
            fp += 1
        else:
            fn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if precision + recall else 0.0)
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 3),
            "recall": round(recall, 3), "f1": round(f1, 3)}


def run(docs: list[dict], model, tokenizer, deciders: list[str],
        thresholds: list[float], null_margin: float = 1.0) -> dict:
    """Run the canonical and cause questions once, then score each trigger."""
    from ..specialist.qa import CUAD_QUESTIONS, best_spans

    canonical: dict[str, str | None] = {}
    cause: dict[str, str | None] = {}
    jev_scores: dict[str, float] = {}
    for doc in docs:
        doc_id, context = doc["doc_id"], doc["context"]
        span = best_spans(model, tokenizer, CUAD_QUESTIONS["termination"],
                          context, null_margin=null_margin)
        canonical[doc_id] = span.text if span else None
        if canonical[doc_id] is None:
            span = best_spans(model, tokenizer, CAUSE_TERMINATION_QUESTION,
                              context, null_margin=null_margin)
            cause[doc_id] = span.text if span else None
        else:
            cause[doc_id] = None
    if "jev" in deciders:
        for doc in docs:
            jev_scores[doc["doc_id"]] = cause_jev_score(doc["context"])

    result: dict = {
        "corpus": "CUAD test split",
        "documents": len(docs),
        "termination_present": sum(doc["truth"] is not None for doc in docs),
        "measurement": "offline static batch, local CPU; specialist weights",
        "null_margin": null_margin,
        "guardrail_f1": GUARDRAIL_F1,
        "deciders": {},
        "docs": [],
    }
    for doc in docs:
        result["docs"].append({
            "doc_id": doc["doc_id"],
            "truth": (doc["truth"] or "")[:120] or None,
            "canonical": (canonical[doc["doc_id"]] or "")[:120] or None,
            "cause": (cause[doc["doc_id"]] or "")[:120] or None,
            "regex_trigger": cause_language_present(doc["context"], "regex"),
            "jev_score": round(jev_scores[doc["doc_id"]], 4)
            if doc["doc_id"] in jev_scores else None,
        })
    for decider in deciders:
        if decider == "regex":
            rows = [(doc["truth"],
                     canonical[doc["doc_id"]] if canonical[doc["doc_id"]]
                     else cause[doc["doc_id"]]
                     if cause_language_present(doc["context"], "regex") else None)
                    for doc in docs]
            result["deciders"]["regex"] = {"threshold": None,
                                           **score_rows(rows)}
            continue
        for threshold in thresholds:
            rows = []
            for doc in docs:
                doc_id = doc["doc_id"]
                got = canonical[doc_id]
                if got is None and jev_scores[doc_id] >= threshold:
                    got = cause[doc_id]
                rows.append((doc["truth"], got))
            result["deciders"][f"jev@{threshold:g}"] = {
                "threshold": threshold, **score_rows(rows)}
    return result


def format_table(result: dict) -> str:
    lines = [
        f"corpus: {result['corpus']}  documents: {result['documents']}  "
        f"termination present: {result['termination_present']}",
        result["measurement"],
        f"guardrail: F1 >= {result['guardrail_f1']:.3f} (regex baseline)",
        "",
        f"{'decider':<12} {'tp':>4} {'fp':>4} {'fn':>4} "
        f"{'precision':>9} {'recall':>7} {'f1':>7} {'passes':>7}",
    ]
    for name, row in result["deciders"].items():
        lines.append(
            f"{name:<12} {row['tp']:>4} {row['fp']:>4} {row['fn']:>4} "
            f"{row['precision']:>9.3f} {row['recall']:>7.3f} "
            f"{row['f1']:>7.3f} "
            f"{'yes' if row['f1'] >= result['guardrail_f1'] else 'no':>7}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="data/cuad-models/roberta-large")
    parser.add_argument("--data", default="data/cuad-test",
                        help="CUAD test corpus with ground_truth.json and documents/")
    parser.add_argument("--docs", type=int, default=None,
                        help="limit documents (default: all unique titles)")
    parser.add_argument("--null-margin", type=float, default=1.0,
                        help="same no-answer margin the specialist serves with")
    parser.add_argument("--deciders", nargs="+", default=["regex", "jev"],
                        choices=["regex", "jev"])
    parser.add_argument("--thresholds", nargs="+", type=float,
                        default=list(DEFAULT_THRESHOLDS))
    parser.add_argument("--json", type=Path, help="write the full result as JSON")
    args = parser.parse_args()

    import torch
    from transformers import AutoModelForQuestionAnswering, AutoTokenizer

    docs = load_cuad_docs(args.data, args.docs)
    tokenizer = AutoTokenizer.from_pretrained(args.model, use_fast=False)
    model = AutoModelForQuestionAnswering.from_pretrained(
        args.model, dtype=torch.float32)
    model.eval()
    result = run(docs, model, tokenizer, list(args.deciders),
                 list(args.thresholds), null_margin=args.null_margin)
    result["model"] = args.model
    print(format_table(result))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2) + "\n")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
