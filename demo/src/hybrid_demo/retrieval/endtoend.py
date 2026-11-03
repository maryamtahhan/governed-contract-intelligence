"""End-to-end retrieve-then-extract measurement (FINDINGS §11).

The test that justifies the retrieval-first pivot: does the CUAD specialist
(RoBERTa-large QA, cannot hallucinate - it returns spans of its input) fed
only the top-k chunks retrieved by litil-embed-0.6b match or beat the
generative extraction baselines (1.5B F1 0.242, 35B-A3B MoE 0.712 on the
same fields)?

Two arms, scored with the pipeline's own scorer (evalkit.score) on exactly
the fields the baselines were scored on (the extract: steps of a recorded
results.json, same ground truth, same text matching):

  full-doc — the specialist sees the whole contract: the no-retrieval
             upper bound, and the local precursor of the interrupted
             test-split specialist arm.
  top-k    — the specialist sees only the top-k chunks (default 5)
             retrieved by the embedder: the pipeline number.

Also decomposes errors: for fields with ground truth, a miss is either a
retrieval miss (no top-k chunk overlaps any expert-annotated span) or a
specialist miss (the span was in the window and not returned).

    cd demo && uv run --group retrieval hybrid-retrieval-e2e \
        --baseline-run ../runs/cuad-test/arm-D-moe/results.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from ..evalkit.score import extraction_scores, text_matches
from ..specialist.qa import CLAUSE_ALIASES, best_spans, question_for
from .chunking import chunk_document
from .evaluate import annotated_pairs, load_test_split


def load_baseline_fields(results_json: Path) -> list[tuple[str, str]]:
    """(doc_id, clause) pairs the generative baselines were scored on."""
    data = json.loads(results_json.read_text())
    return [(r["doc_id"], r["step"].split(":", 1)[1])
            for r in data["records"] if r["step"].startswith("extract:")]


def load_truth(data_dir: Path) -> dict[str, dict]:
    manifest = json.loads((data_dir / "ground_truth.json").read_text())
    return {d["doc_id"]: d for d in manifest["documents"]}


def doc_id_of(title: str) -> str:
    """The converter's doc_id derivation (pipeline/cuad.py)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", title)[:48]


def annotated_by_doc(cuad_zip: Path) -> dict[str, dict[str, list[tuple[int, int]]]]:
    """doc_id -> category -> annotated spans (offsets valid in the .txt)."""
    out: dict[str, dict[str, list[tuple[int, int]]]] = {}
    for doc in load_test_split(cuad_zip):
        out[doc_id_of(doc["title"])] = {cat: spans
                                        for cat, spans in annotated_pairs(doc)}
    return out


def _record(doc_id: str, clause: str, span) -> dict:
    return {"step": f"extract:{clause}", "doc_id": doc_id,
            "content": {"found": span is not None,
                        "text": span.text if span else None}}


def _decompose(records: list[dict], truth: dict, covered: dict) -> dict:
    """Split misses into retrieval misses vs specialist misses."""
    fn_retrieval = fn_specialist = fp_unannotated = tp = fp = fn = 0
    for r in records:
        clause = r["step"].split(":", 1)[1]
        doc = r["doc_id"]
        gold = (truth.get(doc, {}).get("clauses") or {}).get(clause)
        found, text = r["content"]["found"], r["content"]["text"]
        if gold is None:
            if found:
                fp += 1
                fp_unannotated += 1
            continue
        if found and text_matches(text, gold):
            tp += 1
        elif found:
            fp += 1
        else:
            fn += 1
            if covered.get((doc, clause)):
                fn_specialist += 1
            else:
                fn_retrieval += 1
    return {"tp": tp, "fp": fp, "fn": fn,
            "fn_retrieval_miss": fn_retrieval, "fn_specialist_miss": fn_specialist,
            "fp_on_unannotated": fp_unannotated}


def run(baseline_run: Path, data_dir: Path, cuad_zip: Path, checkpoint: str,
        embedder_name: str, top_k: int, null_margin: float) -> dict:
    from sentence_transformers import SentenceTransformer
    from transformers import AutoModelForQuestionAnswering, AutoTokenizer
    import torch

    truth = load_truth(data_dir)
    fields = load_baseline_fields(baseline_run)
    doc_ids = list(dict.fromkeys(d for d, _ in fields))
    texts = {d: (data_dir / "documents" / f"{d}.txt").read_text()
             for d in doc_ids}
    spans_by_doc = annotated_by_doc(cuad_zip)

    tok = AutoTokenizer.from_pretrained(checkpoint)
    model = AutoModelForQuestionAnswering.from_pretrained(checkpoint,
                                                          dtype=torch.float32)
    model.eval()

    embedder = SentenceTransformer(embedder_name)
    prompts = getattr(embedder, "prompts", None) or {}
    q_name = "query" if "query" in prompts else None
    p_name = next((k for k in ("passage", "document") if k in prompts), None)

    # question_for keys on the aliased clause name (e.g. "governing law"),
    # not the pipeline's clause key ("governing_law") - the specialist
    # server applies the same aliasing before it calls question_for.
    questions = {c: question_for(CLAUSE_ALIASES.get(c, c)) for _, c in fields}
    q_vecs = {c: embedder.encode([q], prompt_name=q_name,
                                 normalize_embeddings=True)[0]
              for c, q in questions.items() if q}

    chunk_vecs: dict[str, list] = {}
    top_chunks: dict[str, list] = {}
    for d in doc_ids:
        chunks = chunk_document(d, texts[d])
        vecs = embedder.encode([c.text for c in chunks], prompt_name=p_name,
                               normalize_embeddings=True)
        chunk_vecs[d] = vecs
        top_chunks[d] = {}
        for c, q in questions.items():
            if q is None or c not in q_vecs:
                continue
            sims = (vecs @ q_vecs[c]).tolist()
            order = sorted(range(len(chunks)), key=lambda i: -sims[i])
            top_chunks[d][c] = [chunks[i] for i in order[:top_k]]

    arms: dict[str, list[dict]] = {"full-doc": [], f"top-{top_k}": []}
    covered: dict[tuple[str, str], bool] = {}

    for doc_id, clause in fields:
        question = questions[clause]
        gold_spans = spans_by_doc.get(doc_id, {}).get(
            _category_of(clause), [])
        window = top_chunks[doc_id].get(clause, [])
        covered[(doc_id, clause)] = any(
            ch.overlaps(s, e) for ch in window for s, e in gold_spans)

        span = best_spans(model, tok, question, texts[doc_id],
                          null_margin=null_margin)
        arms["full-doc"].append(_record(doc_id, clause, span))

        context = "\n\n".join(c.text for c in window)
        span = best_spans(model, tok, question, context,
                          null_margin=null_margin)
        arms[f"top-{top_k}"].append(_record(doc_id, clause, span))

    result = {
        "fields": len(fields),
        "docs": len(doc_ids),
        "checkpoint": checkpoint,
        "embedder": embedder_name,
        "top_k": top_k,
        "null_margin": null_margin,
        "arms": {},
    }
    for arm, records in arms.items():
        result["arms"][arm] = {
            "extraction": extraction_scores(records, truth),
            "decomposition": _decompose(records, truth, covered)
            if arm != "full-doc" else _decompose_allcovered(records, truth),
            "records": records,
        }
    return result


def _category_of(clause: str) -> str:
    """Pipeline clause key -> CUAD category name."""
    return {"termination": "Termination For Convenience",
            "governing_law": "Governing Law",
            "liability_cap": "Cap On Liability",
            "audit_rights": "Audit Rights"}.get(clause, clause)


def _decompose_allcovered(records: list[dict], truth: dict) -> dict:
    """Full-doc arm: every window 'covered' - misses are specialist misses."""
    dec = _decompose(records, truth, {})
    dec["fn_retrieval_miss"] = 0
    dec["fn_specialist_miss"] = dec["fn"]
    return dec


def main() -> None:
    parser = argparse.ArgumentParser(
        description="end-to-end retrieve-then-extract measurement")
    parser.add_argument("--baseline-run", required=True,
                        help="results.json of a recorded arm (defines the "
                             "scored fields)")
    parser.add_argument("--data", default="data/cuad-docs")
    parser.add_argument("--cuad", default="data/cuad/cuad-main/data.zip")
    parser.add_argument("--checkpoint", default="data/cuad-models/roberta-large")
    parser.add_argument("--embedder", default="litillabs/litil-embed-0.6b")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--null-margin", type=float, default=1.0)
    parser.add_argument("--out", default=None,
                        help="write the result JSON here (default: print only)")
    args = parser.parse_args()

    result = run(Path(args.baseline_run), Path(args.data), Path(args.cuad),
                 args.checkpoint, args.embedder, args.top_k, args.null_margin)
    for arm, r in result["arms"].items():
        e = r["extraction"]
        print(f"{arm:<8} P {e['precision']:.3f}  R {e['recall']:.3f}  "
              f"F1 {e['f1']:.3f}  ({e['fields_correct']}/{e['fields_total']} fields)")
        print(f"         {r['decomposition']}")
    print("baselines on the same fields: 1.5B F1 0.242, 35B-A3B MoE F1 0.712")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1))
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
