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
        --data data/cuad-test --out ../runs/cuad-test/arm-direct-moe-gpu \
        --model Qwen/Qwen3.5-35B-A3B

The summary records ``model`` (the checkpoint label passed on the command
line) and ``served_model`` (the alias the endpoint reports on ``/models``).
The OpenAI API can only report the served alias, so the checkpoint has to be
named explicitly to keep a run identifiable later.

It also records provenance: ``--environment``, ``--cluster`` (or
HYBRID_ENVIRONMENT/HYBRID_CLUSTER), the git commit and whether the tree was
dirty, and ``--pools`` — a JSON map of route → model/hardware/deployment.
The endpoint cannot say which hardware it runs on; only the operator can, and
a run without that map can be replayed but not explained.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

from ..common import parse_pools, run_provenance
from ..evalkit.score import docs_fully_correct, extraction_scores, report_score, step_accuracy
from .generate import CLAUSE_KEYS
from .review import classify_body, extract_body, parse_content, report_body, risk_body


async def call_step(client: httpx.AsyncClient, sem: asyncio.Semaphore, step: str,
                    doc_id: str, body: dict) -> dict:
    async with sem:
        started = time.monotonic()
        usage = None
        # The gateway reports which pool answered and under which trace. A bare
        # vLLM endpoint sends none of these, so their absence is the signal
        # that this call bypassed governance — not a rendering gap.
        trace = {}
        response_model = None
        try:
            resp = await client.post("/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage")
            # What the endpoint says answered, per call. The header chip can
            # only show the one model a direct run is pointed at; this is the
            # evidence that every stage really did go there.
            response_model = data.get("model")
            trace = {"route": resp.headers.get("x-demo-route"),
                     "entry": resp.headers.get("x-demo-entry"),
                     "trace_id": resp.headers.get("x-praxis-trace-id")}
            outcome = "served"
        except httpx.HTTPError as e:
            content, outcome = None, f"error: {e.__class__.__name__}"
        return {"step": step, "doc_id": doc_id, "outcome": outcome,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "content": parse_content(content), "usage": usage,
                "response_model": response_model, **trace}


async def _served_model(client: httpx.AsyncClient) -> str | None:
    """The served alias the endpoint reports, if it exposes /models."""
    try:
        resp = await client.get("/models")
        resp.raise_for_status()
        ids = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
        return ids[0] if ids else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


async def run_doc(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                  doc_id: str, text: str, clause_keys: list[str],
                  parallel: bool = False) -> list[dict]:
    """One document through classify, extract per clause, then risk.

    ``parallel`` is for an interactive single document: classify and every
    extract depend only on the document text, so they can go together and the
    chain costs two round trips instead of six. The batch runner keeps the
    sequential order so per-step latencies stay comparable with earlier runs.
    """
    if parallel:
        futures = [call_step(client, sem, "classify", doc_id, classify_body(text))]
        futures += [call_step(client, sem, f"extract:{c}", doc_id, extract_body(text, c))
                    for c in clause_keys]
        results = await asyncio.gather(*futures)
        classify_rec, extract_recs = results[0], results[1:]
        clauses = {c: r["content"].get("text")
                   for c, r in zip(clause_keys, extract_recs, strict=True)}
        risk_rec = await call_step(client, sem, "risk", doc_id,
                                   risk_body(clauses, sensitive=False))
        return [classify_rec, *extract_recs, risk_rec]

    records: list[dict] = []
    records.append(await call_step(client, sem, "classify", doc_id, classify_body(text)))
    clauses: dict = {}
    for clause in clause_keys:
        rec = await call_step(client, sem, f"extract:{clause}", doc_id,
                              extract_body(text, clause))
        clauses[clause] = rec["content"].get("text")
        records.append(rec)
    records.append(await call_step(client, sem, "risk", doc_id,
                                   risk_body(clauses, sensitive=False)))
    return records


async def run_doc_stream(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                         doc_id: str, text: str, clause_keys: list[str]):
    """One document, yielding each record as it completes.

    The interactive order without the wait: classify and every extract go out
    together and come back in completion order, then risk reads the extracts.
    Records come from the same ``call_step`` as ``run_doc(parallel=True)``, so
    a consumer that collects them sees the identical result — this only makes
    the fan-out observable while it happens.
    """
    tasks = [asyncio.create_task(
        call_step(client, sem, "classify", doc_id, classify_body(text)))]
    tasks += [asyncio.create_task(
        call_step(client, sem, f"extract:{c}", doc_id, extract_body(text, c)))
        for c in clause_keys]
    extracts: dict[str, dict] = {}
    for future in asyncio.as_completed(tasks):
        rec = await future
        if rec["step"].startswith("extract:"):
            extracts[rec["step"].split(":", 1)[1]] = rec
        yield rec
    clauses = {c: (extracts.get(c) or {}).get("content", {}).get("text")
               for c in clause_keys}
    yield await call_step(client, sem, "risk", doc_id,
                          risk_body(clauses, sensitive=False))


async def run(endpoint: str, data_dir: str, out_dir: str,
              concurrency: int, timeout: float, docs_limit: int | None,
              model_label: str | None = None,
              environment: str | None = None, cluster: str | None = None,
              pools: dict | None = None,
              doc_ids: list[str] | None = None) -> dict:
    truth = json.loads((Path(data_dir) / "ground_truth.json").read_text())
    truth_docs = {d["doc_id"]: d for d in truth["documents"]}
    if doc_ids:
        wanted = set(doc_ids)
        missing = sorted(wanted - set(truth_docs))
        if missing:
            raise SystemExit(f"doc ids not in {data_dir}: {missing}")
        # manifest order, not argument order: a direct run must stay
        # comparable with a pipeline run over the same selection.
        doc_ids = [d for d in truth_docs if d in wanted]
    else:
        doc_ids = list(truth_docs)[:docs_limit] if docs_limit else list(truth_docs)
    # The dataset names its own clauses; generate.CLAUSE_KEYS is only the
    # fallback for manifests that predate them. Hardcoding the default asked
    # CUAD for data_protection after the manifest moved to audit_rights, so
    # one category went unscored and its truth-less fields scored for free.
    clause_keys = truth.get("clause_keys") or list(CLAUSE_KEYS)

    sem = asyncio.Semaphore(concurrency)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=endpoint, timeout=timeout) as client:
        served_model = await _served_model(client)
        doc_records = await asyncio.gather(*[
            run_doc(client, sem, d,
                    (Path(data_dir) / "documents" / f"{d}.txt").read_text(),
                    clause_keys)
            for d in doc_ids])
        records = [r for doc in doc_records for r in doc]
        # See report_lines(): one compact line per document, not one per
        # extraction record (the latter overflows 16k at 200 documents).
        has_dp = "data_protection" in clause_keys
        # the report request must use the still-open client
        report_content, report_ms = await _report(
            client, report_lines(records, doc_ids), len(doc_ids), has_dp)
    records.append({"step": "report", "doc_id": None, "outcome": "served",
                    "latency_ms": report_ms, "content": parse_content(report_content)})

    summary = {
        "arm": "direct", "mode": "direct", "docs": len(doc_ids),
        "endpoint": endpoint,
        "model": model_label or served_model,
        "served_model": served_model,
        "thinking": __import__("os").environ.get("HYBRID_THINKING") == "1",
        "docs_limit": docs_limit,
        "doc_ids": list(doc_ids),
        "concurrency": concurrency,
        "timeout_s": timeout,
        # Wall time is a property of the run, not of a step: the report used to
        # record latency 0, which made the batch-level step look free.
        "wall_s": round(time.monotonic() - started, 1),
        "started_at": started_at,
        # A direct run has one endpoint, not a routing table; the pool map
        # still names the hardware/checkpoint that endpoint served, which the
        # endpoint itself cannot report.
        **run_provenance(environment, cluster, pools),
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


def report_lines(records: list[dict], doc_ids: list[str]) -> list[str]:
    """One compact line per document for the cross-document report prompt.

    The first version emitted one line per extraction *record*. At 19 CUAD
    documents that fit; at 200 documents it is ~150k chars (~37k tokens) and
    every pool serves max-model-len 16384, so the report request failed with
    HTTP 400 after all the per-document work had been paid for (2026-11-04).
    This mirrors the stacked pipeline's report_body input.
    """
    by_doc: dict[str, dict] = {}
    for r in records:
        doc_id = r.get("doc_id")
        if not doc_id:
            continue
        slot = by_doc.setdefault(doc_id, {})
        step = r.get("step", "")
        content = r.get("content")
        if not isinstance(content, dict):
            continue
        if step.startswith("extract:"):
            slot[step.split(":", 1)[1]] = content
        elif step == "classify":
            slot["type"] = content.get("type")
        elif step == "risk":
            slot["risk"] = content.get("risk")
    lines: list[str] = []
    for doc_id in doc_ids:
        slot = by_doc.get(doc_id, {})
        law_rec = slot.get("governing_law") or {}
        law = law_rec.get("text", "unknown") if law_rec.get("found") else "missing"
        lines.append("- " + json.dumps({
            "ref": doc_id,
            "type": slot.get("type") or "unknown",
            "risk": slot.get("risk") or "unknown",
            # 100 chars covers every governing-law clause in the synthetic
            # set without truncating the jurisdiction; the 200-doc prompt
            # stays at 37.5k chars, the same size the stacked run proved.
            "governing_law": (law or "unknown")[:100],
            "data_protection": bool((slot.get("data_protection") or {}).get("found")),
        }, ensure_ascii=False))
    return lines


async def _report(client: httpx.AsyncClient, lines: list[str],
                  count: int, has_data_protection: bool = True) -> tuple[str, float]:
    started = time.monotonic()
    resp = await client.post(
        "/chat/completions",
        json=report_body(lines, count, has_data_protection),
    )
    if resp.status_code >= 400:
        # raise_for_status() alone hides the server's explanation; the 400s
        # that matter here are context-length rejections.
        raise RuntimeError(f"report step failed: HTTP {resp.status_code}: {resp.text[:500]}")
    latency = round((time.monotonic() - started) * 1000, 1)
    return resp.json()["choices"][0]["message"]["content"], latency


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="run the review pipeline directly against an endpoint")
    parser.add_argument("--endpoint", required=True,
                        help="OpenAI-compatible base URL, e.g. http://host:8000/v1")
    parser.add_argument("--data", default="data/cuad-test")
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--docs", type=int, default=None,
                        help="run the first N documents in manifest order; "
                             "ignored when --doc-ids is set")
    parser.add_argument("--doc-ids", default=None,
                        help="comma-separated document ids; overrides --docs")
    parser.add_argument("--model", default=None,
                        help="checkpoint label recorded in the summary, e.g. "
                             "Qwen/Qwen3.5-35B-A3B; defaults to the endpoint's /models id")
    parser.add_argument("--environment", default=None,
                        help="where this ran, e.g. g5.12xlarge-instance or sno; "
                             "defaults to HYBRID_ENVIRONMENT")
    parser.add_argument("--cluster", default=None,
                        help="cluster identity, e.g. sno-rh-dev; defaults to HYBRID_CLUSTER")
    parser.add_argument("--pools", default=None,
                        help="route → {model, hardware, deployment} JSON, or @path; "
                             "the harness cannot discover what sits behind a route")
    args = parser.parse_args(argv)
    doc_ids = ([d.strip() for d in args.doc_ids.split(",") if d.strip()]
               if args.doc_ids else None)
    scored = asyncio.run(run(args.endpoint, args.data, args.out,
                             args.concurrency, args.timeout, args.docs, args.model,
                             environment=args.environment, cluster=args.cluster,
                             pools=parse_pools(args.pools), doc_ids=doc_ids))
    print(json.dumps(scored, indent=1))


if __name__ == "__main__":
    main()
