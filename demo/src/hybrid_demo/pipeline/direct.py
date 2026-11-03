"""Direct arm runner: the review pipeline's steps against a bare endpoint.

For measurements that isolate model quality from the gateway — e.g. the
thinking-mode A/B served on GPU — this runs the *same* request bodies,
parser, ground truth and scorer as hybrid-review, but the client talks
straight to an OpenAI-compatible endpoint (vLLM). No praxis, no labels,
no routing, no audit trail: results are labelled "direct" and are not
comparable to through-stack arms on governance or routing axes — only on
quality (extraction P/R/F1, risk accuracy, report accuracy).

Thinking mode follows the same switch as the pipeline: set
HYBRID_THINKING=1 for the thinking arm (bigger token budgets, think
blocks stripped by the shared parser).

    uv run hybrid-direct-arm --endpoint http://localhost:8000/v1 \
        --data data/cuad-test --out ../runs/cuad-test/arm-direct-moe-gpu
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

from ..evalkit.score import docs_fully_correct, extraction_scores, report_score, step_accuracy
from .generate import CLAUSE_KEYS
from .review import classify_body, extract_body, parse_content, report_body, risk_body


async def run_doc(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                  doc_id: str, text: str, clause_keys: list[str]) -> list[dict]:
    records: list[dict] = []

    async def call(step: str, body: dict) -> dict:
        async with sem:
            started = time.monotonic()
            try:
                resp = await client.post("/chat/completions", json=body)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                outcome = "served"
            except httpx.HTTPError as e:
                content, outcome = None, f"error: {e.__class__.__name__}"
            return {"step": step, "doc_id": doc_id, "outcome": outcome,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "content": parse_content(content)}

    records.append(await call("classify", classify_body(text)))
    clauses: dict = {}
    for clause in clause_keys:
        rec = await call(f"extract:{clause}", extract_body(text, clause))
        clauses[clause] = rec["content"].get("text")
        records.append(rec)
    records.append(await call("risk", risk_body(clauses, sensitive=False)))
    return records


async def run(endpoint: str, data_dir: str, out_dir: str,
              concurrency: int, timeout: float, docs_limit: int | None) -> dict:
    truth = json.loads((Path(data_dir) / "ground_truth.json").read_text())
    truth_docs = {d["doc_id"]: d for d in truth["documents"]}
    doc_ids = list(truth_docs)[:docs_limit] if docs_limit else list(truth_docs)

    sem = asyncio.Semaphore(concurrency)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(base_url=endpoint, timeout=timeout) as client:
        doc_records = await asyncio.gather(*[
            run_doc(client, sem, d,
                    (Path(data_dir) / "documents" / f"{d}.txt").read_text(),
                    list(CLAUSE_KEYS))
            for d in doc_ids])
        records = [r for doc in doc_records for r in doc]
        lines = [f"{r['doc_id']}: " + json.dumps(r["content"], ensure_ascii=False)
                 for r in records if r["step"].startswith("extract")]
        # the report request must use the still-open client
        report_content = await _report(client, lines, len(doc_ids))
    records.append({"step": "report", "doc_id": None, "outcome": "served",
                    "latency_ms": 0, "content": parse_content(report_content)})

    summary = {
        "arm": "direct", "mode": "direct", "docs": len(doc_ids),
        "endpoint": endpoint, "thinking": __import__("os").environ.get("HYBRID_THINKING") == "1",
    }
    (out / "results.json").write_text(json.dumps(
        {"summary": summary, "records": records}, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))

    scored = {
        "extraction": extraction_scores(records, truth_docs),
        "risk_accuracy": step_accuracy(records, truth_docs, "risk", "risk"),
        "report_accuracy": report_score(records, truth_docs),
        "docs_fully_correct": docs_fully_correct(records, truth_docs),
    }
    (out / "metrics.json").write_text(json.dumps(scored, indent=1))
    return scored


async def _report(client: httpx.AsyncClient, lines: list[str], count: int) -> str:
    resp = await client.post("/chat/completions", json=report_body(lines, count))
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="run the review pipeline directly against an endpoint")
    parser.add_argument("--endpoint", required=True,
                        help="OpenAI-compatible base URL, e.g. http://host:8000/v1")
    parser.add_argument("--data", default="data/cuad-test")
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--docs", type=int, default=None)
    args = parser.parse_args()
    scored = asyncio.run(run(args.endpoint, args.data, args.out,
                             args.concurrency, args.timeout, args.docs))
    print(json.dumps(scored, indent=1))


if __name__ == "__main__":
    main()
