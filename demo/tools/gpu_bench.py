"""Concurrency sweep against an OpenAI-compatible endpoint (the V1 concern).

Measures time-to-first-token and end-to-end latency at increasing concurrency
against an already-warm server. This is the GPU side of the hybrid-bench open
item (FINDINGS §9): prefill-dominated contract prompts at concurrency 1/4/16,
streaming, no gateway in the path, so the numbers are the backend's alone.

    uv run python tools/gpu_bench.py --url http://localhost:8000/v1 \
        --data data/cuad-test --levels 1,4,16

Each level sends `--factor` requests per unit of concurrency, so 1/4/16 with a
factor of 4 is 4/16/64 requests. A warm-up request is excluded from the
statistics. Completion tokens come from the server's streaming usage block, so
a server that ignores `stream_options` reports None instead of guessing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

from hybrid_demo.pipeline.review import extract_body, load_documents


async def one_request(client: httpx.AsyncClient, body: dict) -> dict:
    started = time.monotonic()
    ttft_ms: float | None = None
    completion_tokens: int | None = None
    async with client.stream("POST", "/chat/completions", json=body) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if chunk.get("usage"):
                completion_tokens = chunk["usage"].get("completion_tokens")
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content") and ttft_ms is None:
                    ttft_ms = (time.monotonic() - started) * 1000
    return {
        "ttft_ms": ttft_ms,
        "e2e_ms": (time.monotonic() - started) * 1000,
        "completion_tokens": completion_tokens,
    }


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    idx = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return ordered[idx]


async def level(client: httpx.AsyncClient, body: dict, concurrency: int,
                count: int) -> dict:
    sem = asyncio.Semaphore(concurrency)

    async def task() -> dict:
        async with sem:
            return await one_request(client, body)

    started = time.monotonic()
    results = await asyncio.gather(*[task() for _ in range(count)])
    wall_s = time.monotonic() - started

    ttfts = [r["ttft_ms"] for r in results if r["ttft_ms"] is not None]
    e2es = [r["e2e_ms"] for r in results]
    tokens = [r["completion_tokens"] for r in results if r["completion_tokens"]]
    return {
        "concurrency": concurrency,
        "requests": count,
        "wall_s": round(wall_s, 1),
        "ttft_p50_ms": round(pct(ttfts, 50), 1),
        "ttft_p95_ms": round(pct(ttfts, 95), 1),
        "e2e_p50_ms": round(pct(e2es, 50), 1),
        "e2e_p95_ms": round(pct(e2es, 95), 1),
        "requests_per_s": round(count / wall_s, 2),
        "output_tokens_per_s": (round(sum(tokens) / wall_s, 1) if tokens else None),
        "empty_answers": sum(1 for r in results if r["ttft_ms"] is None),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="OpenAI base URL, e.g. http://host:8000/v1")
    parser.add_argument("--data", default="data/cuad-test")
    parser.add_argument("--levels", default="1,4,16", help="comma-separated concurrency levels")
    parser.add_argument("--factor", type=int, default=4, help="requests per unit of concurrency")
    parser.add_argument("--max-tokens", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--out", help="write the results as JSON here")
    args = parser.parse_args()

    docs, texts, clause_keys = load_documents(args.data, 1)
    if not docs:
        raise SystemExit(f"no documents in {args.data}")
    body = extract_body(texts[docs[0]["doc_id"]], "termination")
    body["max_tokens"] = args.max_tokens
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}

    levels = [int(x) for x in args.levels.split(",")]
    rows = []
    async with httpx.AsyncClient(base_url=args.url, timeout=args.timeout) as client:
        await one_request(client, body)  # warm-up, excluded from the numbers
        for concurrency in levels:
            row = await level(client, body, concurrency, concurrency * args.factor)
            rows.append(row)
            print(json.dumps(row))

    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
