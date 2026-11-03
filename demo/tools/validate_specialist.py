"""Local validation: run the CUAD specialist on the held-out test split and
score it exactly like the pipeline's scorer does (containment matching).

    cd demo && uv run python tools/validate_specialist.py --docs 12
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile

import torch
from transformers import AutoModelForQuestionAnswering, AutoTokenizer

from hybrid_demo.common import normalize_text
from hybrid_demo.pipeline.cuad import CATEGORY_MAP
from hybrid_demo.specialist.qa import CUAD_QUESTIONS, best_spans

QUESTION_RE = re.compile(r'related to "(.+?)" that should be reviewed')

# CUAD category -> the key used by CUAD_QUESTIONS (spaces, not underscores).
CATEGORY_TO_KEY = {category: key.replace("_", " ")
                   for key, category in CATEGORY_MAP.items()}


def text_matches(predicted: str | None, truth: str | None) -> bool:
    if not predicted or not truth:
        return False
    p, t = normalize_text(predicted), normalize_text(truth)
    return p == t or p in t or t in p


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="data/cuad-models/roberta-large")
    parser.add_argument("--cuad", default="data/cuad/cuad-main/data.zip")
    parser.add_argument("--docs", type=int, default=12)
    parser.add_argument("--max-chars", type=int, default=24000)
    args = parser.parse_args()

    with zipfile.ZipFile(args.cuad) as zf:
        full = json.loads(zf.read("CUADv1.json"))["data"]
        test = json.loads(zf.read("test.json"))["data"]

    by_title = {d["title"]: d for d in full}
    usable = [t for t in (d["title"] for d in test)
              if 400 <= len(by_title[t]["paragraphs"][0]["context"]) <= args.max_chars]

    tok = AutoTokenizer.from_pretrained(args.model, use_fast=False)
    model = AutoModelForQuestionAnswering.from_pretrained(args.model, dtype=torch.float32)
    model.eval()

    fields = []
    for title in usable[: args.docs]:
        para = by_title[title]["paragraphs"][0]
        context = para["context"]
        truth = {}
        for qa in para["qas"]:
            m = QUESTION_RE.search(qa["question"])
            if not m:
                continue
            category = m.group(1).strip()
            key = CATEGORY_TO_KEY.get(category)
            if key and key in CUAD_QUESTIONS and key not in truth:
                answers = [a["text"] for a in qa.get("answers", []) if a.get("text")]
                truth[key] = answers[0] if answers else None

        for key in CUAD_QUESTIONS:
            span = best_spans(model, tok, CUAD_QUESTIONS[key], context)
            want = truth.get(key)
            fields.append((key, want, span.text if span else None,
                           span.score if span else None, span.null_score if span else None))

    print(f"docs={args.docs} fields={len(fields)}")
    for margin in (0.0, 1.0, 2.0, 3.0, 4.0):
        tp = fp = fn = 0
        for _key, want, got, score, null in fields:
            if got is not None and null is not None and score is not None \
                    and score < null + margin:
                got = None
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
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        print(f"margin={margin:.1f}  tp={tp} fp={fp} fn={fn}  "
              f"P={precision:.3f} R={recall:.3f} F1={f1:.3f}")
    print("-- FP examples (answered on annotation-absent clauses):")
    shown = 0
    for key, want, got, _s, _n in fields:
        if want is None and got and shown < 6:
            print(f"  [{key}] got: {got[:110]}")
            shown += 1
    print("-- per-clause present/absent:")
    from collections import Counter
    present = Counter(k for k, w, *_ in fields if w)
    absent = Counter(k for k, w, *_ in fields if not w)
    for k in CUAD_QUESTIONS:
        print(f"  {k:15} present={present.get(k, 0)} absent={absent.get(k, 0)}")


if __name__ == "__main__":
    main()
