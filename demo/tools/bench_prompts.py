"""Dump real pipeline prompts as JSONL for `vllm bench serve --dataset-name custom`.

`vllm bench serve` can only time requests; it never scores an answer. The CUAD
quality numbers come from `hybrid-direct-arm`. This tool supplies the other half:
the *same* prompts the pipeline sends, so the performance sweep runs on real
contract text and real prefill lengths instead of synthetic random tokens.

    uv run python tools/bench_prompts.py --data data/cuad-test --out prompts.jsonl
    uv run python tools/bench_prompts.py --mix --out mixed.jsonl

`--mix` emits the whole per-document sequence (classify, one extract per clause,
risk) plus the cross-document report: the same 115 requests `hybrid-direct-arm`
sends, with each step's own output budget. Without it only extract prompts are
dumped, which is the prefill-dominated slice that dominates the batch.

`CustomDataset` accepts one user message per row, so the system prompt is
flattened into the prompt text. Measure both against a raw request before
quoting absolute prompt lengths; the flattening changes a few template tokens.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hybrid_demo.pipeline.review import (
    classify_body,
    extract_body,
    load_documents,
    report_body,
    risk_body,
)

# The pipeline's per-step output budgets (pipeline/review.py STEP_TOKENS).
OUTPUT_TOKENS = {"classify": 100, "extract": 200, "risk": 200, "report": 3000}


def prompt_text(body: dict) -> str:
    return "\n\n".join(m["content"] for m in body["messages"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default="data/cuad-test")
    parser.add_argument("--out", required=True)
    parser.add_argument("--clause", default="termination",
                        help="single clause, or --all-clauses for every clause key")
    parser.add_argument("--all-clauses", action="store_true")
    parser.add_argument("--mix", action="store_true",
                        help="emit classify/extract/risk per document plus the report")
    parser.add_argument("--limit", type=int, help="only the first N documents")
    parser.add_argument("--output-tokens", type=int, default=200)
    parser.add_argument("--run", help="recorded results.json directory: replay its risk/report "
                                      "prompts so the heavy requests keep their real length")
    args = parser.parse_args()

    docs, texts, clause_keys = load_documents(args.data, args.limit)
    if not docs:
        raise SystemExit(f"no documents in {args.data}")
    # The CUAD test manifest carries one document twice; benchmark each text
    # once so the prompt mix matches the 19 documents a scored run covers.
    docs = list({d["doc_id"]: d for d in docs}.values())
    clauses = clause_keys if args.all_clauses else [args.clause]

    records = None
    if args.run:
        records = json.loads((Path(args.run) / "results.json").read_text())["records"]

    def replayed(doc_id: str) -> dict[str, str | None]:
        if not records:
            truth = next(d for d in docs if d["doc_id"] == doc_id).get("clauses") or {}
            return {k: truth.get(k) for k in clauses}
        found: dict[str, str | None] = {}
        for r in records:
            if r.get("doc_id") == doc_id and r["step"].startswith("extract:"):
                found[r["step"].split(":", 1)[1]] = (r.get("content") or {}).get("text")
        return found

    rows = []
    for doc in docs:
        text = texts[doc["doc_id"]]
        if args.mix:
            rows.append({
                "prompt": prompt_text(classify_body(text)),
                "output_tokens": OUTPUT_TOKENS["classify"],
                "doc_id": doc["doc_id"], "step": "classify",
            })
        for clause in clauses:
            rows.append({
                "prompt": prompt_text(extract_body(text, clause)),
                "output_tokens": OUTPUT_TOKENS["extract"] if args.mix else args.output_tokens,
                "doc_id": doc["doc_id"],
                "step": f"extract:{clause}" if args.mix else "extract",
                "clause": clause,
            })
        if args.mix:
            known = replayed(doc["doc_id"])
            rows.append({
                "prompt": prompt_text(risk_body(known, sensitive=False)),
                "output_tokens": OUTPUT_TOKENS["risk"],
                "doc_id": doc["doc_id"], "step": "risk",
            })

    if args.mix:
        if records:
            # Same lines hybrid-direct-arm builds for its report request.
            lines = [f"{r['doc_id']}: " + json.dumps(r["content"], ensure_ascii=False)
                     for r in records if r["step"].startswith("extract")]
        else:
            lines = [f"{d['doc_id']}: " + json.dumps(d.get("clauses") or {},
                                                     ensure_ascii=False)
                     for d in docs]
        rows.append({
            "prompt": prompt_text(report_body(lines, len(docs))),
            "output_tokens": OUTPUT_TOKENS["report"],
            "doc_id": None, "step": "report",
        })

    Path(args.out).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(f"{len(rows)} prompts from {len(docs)} documents -> {args.out}")


if __name__ == "__main__":
    main()
