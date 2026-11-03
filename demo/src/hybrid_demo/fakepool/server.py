from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import time

import uvicorn
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from ..common import est_tokens, new_id, sha256_hex

BLOCK = 128
CLAUSE_LABELS = ["termination", "governing law", "liability cap", "data protection"]
CONTRACT_TYPES = {
    "MASTER SERVICES AGREEMENT": "MSA",
    "NON-DISCLOSURE AGREEMENT": "NDA",
    "STATEMENT OF WORK": "SOW",
    "DATA PROCESSING AGREEMENT": "DPA",
}

TASKS = [
    ("cross_document_report", re.compile(r"cross-document risk report|across (?:all|the) \d+ documents", re.I)),
    ("risk_assessment", re.compile(r"assess the overall risk", re.I)),
    ("clause_extraction", re.compile(r"extract the ([a-z_ ]+?) clause", re.I)),
    ("document_classification", re.compile(r"classify the following contract", re.I)),
]

TASK_TIER = {
    "clause_extraction": "simple",
    "document_classification": "simple",
    "risk_assessment": "complex",
    "cross_document_report": "reasoning",
    "generic": "simple",
}


def detect_task(prompt: str) -> str:
    for name, pattern in TASKS:
        if pattern.search(prompt):
            return name
    return "generic"


def clause_line(prompt: str, label: str) -> str | None:
    m = re.search(rf"(?mi)^{re.escape(label)}:\s*(.+)$", prompt)
    return m.group(1).strip() if m else None


def contract_ref(prompt: str) -> str | None:
    m = re.search(r"Contract Reference:\s*(\S+)", prompt)
    return m.group(1) if m else None


def risk_from_clauses(clauses: dict) -> str:
    dp = clauses.get("data_protection") or {}
    term = clauses.get("termination") or {}
    liab = clauses.get("liability_cap") or {}
    dp_text = dp.get("text") if isinstance(dp, dict) else dp
    if dp.get("found") is False or not dp_text:
        return "high"
    liab_text = liab.get("text") if isinstance(liab, dict) else liab
    if liab_text and "unlimited" in str(liab_text).lower():
        return "high"
    term_text = term.get("text") if isinstance(term, dict) else term
    if term_text and "only for material breach" in str(term_text).lower():
        return "medium"
    return "low"


def extract_json_after(prompt: str, marker: str) -> dict | None:
    idx = prompt.find(marker)
    if idx < 0:
        return None
    rest = prompt[idx + len(marker) :]
    start = rest.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(rest)):
        if rest[i] == "{":
            depth += 1
        elif rest[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(rest[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def simulate_answer(prompt: str, model: str, error_rates: dict[str, float]) -> tuple[str, str]:
    task = detect_task(prompt)
    rng = random.Random(sha256_hex(prompt + model))
    p = error_rates.get(TASK_TIER.get(task, "simple"), 0.05)

    if task == "clause_extraction":
        label_m = re.search(r"extract the ([a-z_ ]+?) clause", prompt, re.I)
        label = label_m.group(1).strip().lower().replace("_", " ") if label_m else "termination"
        text = clause_line(prompt, label)
        ref = contract_ref(prompt)
        found = text is not None
        out_text = text
        if found and rng.random() < p:
            mode = rng.random()
            if mode < 0.5:
                found, out_text = False, None
            else:
                others = [c for c in CLAUSE_LABELS if c != label and clause_line(prompt, c)]
                out_text = clause_line(prompt, rng.choice(others)) if others else None
                found = out_text is not None
        if rng.random() < p / 2 and ref:
            ref = "C-2026-" + str(rng.randint(10000, 99999))
        answer = {"clause": label, "found": found, "text": out_text, "ref": ref}
    elif task == "document_classification":
        type_hit = None
        for heading, code in CONTRACT_TYPES.items():
            if re.search(rf"(?m)^{heading}$", prompt):
                type_hit = code
                break
        term_m = re.search(r"Initial Term:\s*(\d+)\s*months", prompt)
        term = int(term_m.group(1)) if term_m else 0
        if rng.random() < p and type_hit:
            type_hit = rng.choice([c for c in CONTRACT_TYPES.values() if c != type_hit])
        if rng.random() < p / 2 and term:
            term = rng.choice([12, 24, 36])
        answer = {"type": type_hit or "UNKNOWN", "initial_term_months": term, "ref": contract_ref(prompt)}
    elif task == "risk_assessment":
        clauses = extract_json_after(prompt, "Extracted clauses:") or {}
        risk = risk_from_clauses(clauses)
        if rng.random() < p:
            risk = rng.choice([r for r in ("low", "medium", "high") if r != risk])
        reasons = []
        dp = clauses.get("data_protection") or {}
        if isinstance(dp, dict) and (dp.get("found") is False or not dp.get("text")):
            reasons.append("no data-protection clause")
        term = clauses.get("termination") or {}
        if isinstance(term, dict) and "only for material breach" in str(term.get("text", "")).lower():
            reasons.append("termination for cause only")
        liab = clauses.get("liability_cap") or {}
        if isinstance(liab, dict) and "unlimited" in str(liab.get("text", "")).lower():
            reasons.append("unlimited liability")
        if not reasons:
            reasons.append("standard terms")
        answer = {"risk": risk, "reasons": reasons, "ref": contract_ref(prompt)}
    elif task == "cross_document_report":
        docs = []
        for line in prompt.splitlines():
            line = line.strip()
            if line.startswith("- {") and line.endswith("}"):
                try:
                    docs.append(json.loads(line[2:]))
                except json.JSONDecodeError:
                    continue
        kept = []
        for d in docs:
            if rng.random() < p:
                if rng.random() < 0.5:
                    continue
                d = dict(d)
                d["risk"] = rng.choice(["low", "medium", "high"])
            kept.append(d)
        risk_counts = {"low": 0, "medium": 0, "high": 0}
        law_counts: dict[str, int] = {}
        missing_dp = 0
        highest = None
        for d in kept:
            risk = d.get("risk", "low")
            risk_counts[risk] = risk_counts.get(risk, 0) + 1
            law = d.get("governing_law") or "unknown"
            law_counts[law] = law_counts.get(law, 0) + 1
            if not d.get("data_protection"):
                missing_dp += 1
            if risk == "high" and (highest is None or str(d.get("ref", "")) < str(highest)):
                highest = d.get("ref")
        answer = {
            "documents": len(kept),
            "risk_counts": risk_counts,
            "governing_law_counts": law_counts,
            "missing_data_protection": missing_dp,
            "highest_risk_ref": highest,
        }
    else:
        answer = {"summary": "acknowledged", "chars": len(prompt)}
    return json.dumps(answer, ensure_ascii=False), task


class PrefixCache:
    def __init__(self, max_blocks: int = 4096) -> None:
        self.max_blocks = max_blocks
        self.blocks: list[int] = []
        self.block_set: set[int] = set()

    def cached_chars(self, prompt: str) -> int:
        chars = 0
        for i in range(0, len(prompt), BLOCK):
            h = int.from_bytes(hashlib.sha1(prompt[i : i + BLOCK].encode()).digest()[:8], "big")
            if h in self.block_set:
                chars += min(BLOCK, len(prompt) - i)
            else:
                break
        return chars

    def put(self, prompt: str) -> None:
        for i in range(0, len(prompt), BLOCK):
            h = int.from_bytes(hashlib.sha1(prompt[i : i + BLOCK].encode()).digest()[:8], "big")
            if h not in self.block_set:
                self.block_set.add(h)
                self.blocks.append(h)
        while len(self.blocks) > self.max_blocks:
            old = self.blocks.pop(0)
            self.block_set.discard(old)


def parse_error_rates(spec: str) -> dict[str, float]:
    rates: dict[str, float] = {}
    for part in (spec or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                rates[k.strip()] = float(v)
            except ValueError:
                pass
    return rates


def create_app(args: argparse.Namespace) -> FastAPI:
    registry = CollectorRegistry()
    labels = {"pool": args.pool, "replica": f"{args.host}:{args.port}"}
    requests_total = Counter("pool_requests_total", "requests served", ["pool", "replica", "task"],
                             registry=registry)
    busy_seconds = Counter("pool_busy_seconds_total", "simulated device busy time", ["pool", "replica"],
                           registry=registry)
    queue_seconds = Counter("pool_queue_seconds_total", "time spent waiting for the device", ["pool", "replica"],
                            registry=registry)
    tokens_total = Counter("pool_tokens_total", "tokens processed", ["pool", "replica", "direction"],
                           registry=registry)
    prefix_hits = Counter("pool_prefix_cache_hits_total", "prefix blocks served from cache", ["pool", "replica"],
                          registry=registry)
    inflight = Gauge("pool_inflight", "in-flight requests", ["pool", "replica"], registry=registry)
    ttft = Histogram("pool_ttft_seconds", "time to first token (simulated)", ["pool", "replica"],
                     registry=registry)

    semaphore = asyncio.Semaphore(args.concurrency)
    prefix_cache = PrefixCache()
    error_rates = parse_error_rates(args.error_rates)
    app = FastAPI(docs_url=None, redoc_url=None)
    app.title = f"fake-vllm {args.pool}"

    @app.get("/healthz")
    @app.get("/health")
    async def healthz():
        return {"status": "ok", "pool": args.pool, "model": args.model}

    @app.get("/metrics")
    async def metrics():
        return JSONResponse(content=generate_latest(registry).decode(), media_type="text/plain")

    @app.post("/v1/chat/completions")
    async def completions(request: Request, body: dict = Body(...)):
        messages = body.get("messages") or []
        prompt = "\n".join(
            m.get("content", "") for m in messages if isinstance(m, dict) and isinstance(m.get("content"), str)
        )
        content, task = simulate_answer(prompt, args.model, error_rates)
        prompt_tokens = est_tokens(prompt)
        completion_tokens = est_tokens(content)

        t_queue0 = time.monotonic()
        cached_chars = 0
        async with semaphore:
            queue_ms = (time.monotonic() - t_queue0) * 1000.0
            inflight.labels(**labels).inc()
            try:
                cached_chars = prefix_cache.cached_chars(prompt)
                cached_frac = (cached_chars / len(prompt)) if prompt else 0.0
                prefill_tokens = int(prompt_tokens * (1.0 - cached_frac))
                prefill_ms = args.prefill_ms_per_ktok * prefill_tokens / 1000.0
                decode_ms = args.decode_ms_per_tok * completion_tokens
                busy_ms = prefill_ms + decode_ms
                ttft_ms = queue_ms + prefill_ms
                await asyncio.sleep((ttft_ms + decode_ms) * args.time_scale / 1000.0)
                prefix_cache.put(prompt)
            finally:
                inflight.labels(**labels).dec()

        requests_total.labels(pool=args.pool, replica=labels["replica"], task=task).inc()
        busy_seconds.labels(**labels).inc(busy_ms / 1000.0)
        queue_seconds.labels(**labels).inc(queue_ms / 1000.0)
        tokens_total.labels(pool=args.pool, replica=labels["replica"], direction="prompt").inc(prompt_tokens)
        tokens_total.labels(pool=args.pool, replica=labels["replica"], direction="completion").inc(
            completion_tokens)
        if cached_chars:
            prefix_hits.labels(**labels).inc(1)
        ttft.labels(**labels).observe(ttft_ms / 1000.0)

        response = {
            "id": new_id("chatcmpl"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or args.model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        resp = JSONResponse(content=response)
        resp.headers["X-Busy-Ms"] = f"{busy_ms:.1f}"
        resp.headers["X-Queue-Ms"] = f"{queue_ms:.1f}"
        resp.headers["X-Ttft-Ms"] = f"{ttft_ms:.1f}"
        resp.headers["X-Prompt-Tokens"] = str(prompt_tokens)
        resp.headers["X-Completion-Tokens"] = str(completion_tokens)
        resp.headers["X-Prefix-Cached-Chars"] = str(cached_chars)
        resp.headers["X-Pool"] = args.pool
        resp.headers["X-Replica"] = labels["replica"]
        return resp

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="simulated vLLM pool replica (OpenAI-compatible subset)")
    parser.add_argument("--pool", required=True, help="pool name, e.g. vllm-cpu")
    parser.add_argument("--model", required=True, help="model name to report")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--prefill-ms-per-ktok", type=float, default=700.0)
    parser.add_argument("--decode-ms-per-tok", type=float, default=24.0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--time-scale", type=float, default=1.0,
                        help="multiply simulated latencies (tests use e.g. 0.02)")
    parser.add_argument("--error-rates", default="simple=0.05,medium=0.1,complex=0.3,reasoning=0.5",
                        help="per-task answer corruption rates, k=v pairs")
    args = parser.parse_args()
    app = create_app(args)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
