"""OpenAI-compatible server for the CUAD specialist pool.

A composite pool: extraction prompts ("Extract the <clause> clause...") are
answered by the fine-tuned QA checkpoint in a single encoder pass; every
other prompt (classification, risk, report) is forwarded to a general model
served by vLLM (FORWARD_TO). This keeps the pool drop-in behind the trace-ui
relay with no changes to praxis or the pipeline.

The QA answer is formatted to the pipeline's JSON contract:
    {"clause": ..., "found": true/false, "text": ...}
Because the model can only return spans of the input contract, it cannot
hallucinate a quote - its only failure mode is picking the wrong span.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx
import uvicorn
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

from ..common import new_id
from .cache import create_cache
from .qa import best_spans, cause_question_for, clause_from_prompt, question_for

POOL = "vllm-cpu-specialist"


def create_app(args: argparse.Namespace) -> FastAPI:
    import torch
    from transformers import AutoModelForQuestionAnswering, AutoTokenizer

    registry = CollectorRegistry()
    requests_total = Counter("specialist_requests_total", "requests served",
                             ["pool", "task"], registry=registry)
    inflight = Gauge("vllm_num_requests_running", "in-flight requests",
                     ["pool"], registry=registry)
    prompt_tokens = Counter("vllm_prompt_tokens_total", "prompt tokens",
                            ["pool"], registry=registry)
    generation_tokens = Counter("vllm_generation_tokens_total", "completion tokens",
                                ["pool"], registry=registry)
    device_ms = Counter("specialist_device_ms_total", "encoder time in ms",
                        ["pool"], registry=registry)

    tok = AutoTokenizer.from_pretrained(args.model, use_fast=args.fast_tokenizer)
    model = AutoModelForQuestionAnswering.from_pretrained(args.model, dtype=torch.float32)
    model.eval()
    semaphore = asyncio.Semaphore(args.concurrency)
    client = httpx.AsyncClient(timeout=600.0)
    cache = (create_cache(args.cache_backend, threshold=args.cache_threshold,
                          max_entries=args.cache_entries,
                          redis_url=args.cache_redis_url)
             if args.semantic_cache else None)
    app = FastAPI(docs_url=None, redoc_url=None)
    app.title = f"cuad-specialist ({POOL})"

    @app.get("/health")
    @app.get("/healthz")
    async def health():
        return {"status": "ok", "pool": POOL, "model": args.model,
                "forwards_to": args.forward_to,
                "semantic_cache": cache.snapshot() if cache else None}

    @app.get("/metrics")
    async def metrics():
        # Prometheus text format. JSONResponse would quote the payload and
        # escape newlines even with a text/plain media type, which no scraper
        # can parse - this silently left CPU device-time unmeasured.
        return PlainTextResponse(generate_latest(registry).decode(),
                                 media_type="text/plain; version=0.0.4")

    @app.post("/v1/chat/completions")
    async def completions(request: Request, body: dict = Body(...)):
        messages = body.get("messages") or []
        prompt = "\n".join(m.get("content", "") for m in messages
                           if isinstance(m, dict) and isinstance(m.get("content"), str))
        clause = clause_from_prompt(messages)
        question = question_for(clause) if clause else None
        context = _contract_text(prompt)

        if question is None or context is None:
            return await _forward(body)

        if cache is not None:
            cached, served_as = await asyncio.to_thread(
                cache.get, clause, context)
            if cached is not None:
                requests_total.labels(pool=POOL, task=f"cache_{served_as}").inc()
                return JSONResponse(content=_completion(json.dumps(cached, ensure_ascii=False)))

        started = time.monotonic()
        inflight.labels(pool=POOL).inc()
        try:
            async with semaphore:
                span = await asyncio.to_thread(
                    best_spans, model, tok, question, context,
                    null_margin=args.null_margin,
                )
                if span is None:
                    followup = cause_question_for(clause, context)
                    if followup:
                        requests_total.labels(
                            pool=POOL, task="clause_extraction_cause").inc()
                        span = await asyncio.to_thread(
                            best_spans, model, tok, followup, context,
                            null_margin=args.null_margin,
                        )
        finally:
            inflight.labels(pool=POOL).dec()
        device_ms.labels(pool=POOL).inc((time.monotonic() - started) * 1000)

        answer = {
            "clause": clause,
            "found": span is not None,
            "text": span.text if span else None,
            "score": round(span.score, 2) if span else None,
        }
        if cache is not None:
            await asyncio.to_thread(cache.put, clause, context, answer)
        content = json.dumps(answer, ensure_ascii=False)
        requests_total.labels(pool=POOL, task="clause_extraction").inc()
        prompt_tokens.labels(pool=POOL).inc(max(1, len(prompt) // 4))
        generation_tokens.labels(pool=POOL).inc(max(1, len(content) // 4))
        return JSONResponse(content=_completion(content))

    async def _forward(body: dict) -> JSONResponse:
        if not args.forward_to:
            return JSONResponse(status_code=503, content={
                "error": {"message": "specialist pool only answers extraction prompts",
                          "type": "unsupported"}})
        requests_total.labels(pool=POOL, task="forwarded").inc()
        resp = await client.post(args.forward_to.rstrip("/") + "/v1/chat/completions",
                                 json=body)
        return JSONResponse(content=resp.json(), status_code=resp.status_code,
                            headers={"X-Specialist-Forwarded": "true"})

    def _completion(content: str) -> dict:
        return {
            "id": new_id("chatcmpl"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "cuad-specialist",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    return app


def _contract_text(prompt: str) -> str | None:
    """The contract body sits between the instruction and the Return JSON."""
    start = prompt.find("\n\n")
    end = prompt.rfind("\n\nReturn JSON")
    if start < 0 or end <= start:
        return None
    return prompt[start + 2:end].strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="CUAD specialist pool")
    parser.add_argument("--model", required=True, help="path or repo id of the QA checkpoint")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--null-margin", type=float, default=1.0)
    parser.add_argument("--forward-to", default=None,
                        help="general-model endpoint for non-extraction prompts "
                             "(e.g. http://cpureal-vllm-cpu:8000)")
    parser.add_argument("--fast-tokenizer", action="store_true",
                        help="DeBERTa checkpoints need the slow tokenizer")
    parser.add_argument("--semantic-cache", action="store_true",
                        help="exact + similarity response cache for extraction "
                             "prompts; never stores or serves sensitive content")
    parser.add_argument("--cache-threshold", type=float, default=0.98)
    parser.add_argument("--cache-entries", type=int, default=2048)
    parser.add_argument("--cache-backend", default="memory",
                        choices=("memory", "redis"),
                        help="memory (default) or redis for a shared cache "
                             "(requires the 'cache' extra and --cache-redis-url)")
    parser.add_argument("--cache-redis-url", default="redis://localhost:6379")
    args = parser.parse_args()
    app = create_app(args)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
