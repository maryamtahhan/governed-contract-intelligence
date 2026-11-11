"""Jev-compatible System One service for the ``jev-cpu`` pool.

This is deliberately a *model endpoint*, not a router and not the pipeline's
screen.  Praxis owns the work-to-pool decision; the service answers the
System One questions it is given (one ``noul`` probability per question) and
returns 400 for unsupported shapes, so a misroute stays visible.

The open-source backend is Laya (the ``jev`` extra); TypeSafe Jev can sit
behind the same shape.  The service is CPU-only, one model, and exposes
vLLM-style metrics plus ``X-Busy-Ms`` so the trace UI can show device time.
It is reached through the trace-ui relay and the Praxis ``screen`` listener:
the pipeline never calls it directly.

    hybrid-jev --host 0.0.0.0 --port 8000 --model english
"""
from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time

import uvicorn
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from .. import pii

POOL = "jev-cpu"
_MODEL_LOCK = threading.Lock()


def create_app(args: argparse.Namespace) -> FastAPI:
    registry = CollectorRegistry()
    requests_total = Counter("jev_requests_total", "System One requests served",
                             ["pool", "status"], registry=registry)
    questions_total = Counter("jev_questions_total", "questions answered",
                              ["pool"], registry=registry)
    inflight = Gauge("vllm_num_requests_running", "in-flight requests",
                     ["pool"], registry=registry)
    latency = Histogram("jev_request_seconds", "request wall time",
                        ["pool"], registry=registry)
    device_ms = Counter("jev_device_ms_total", "model time in ms",
                        ["pool"], registry=registry)
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    app = FastAPI(docs_url=None, redoc_url=None)
    app.title = f"jev ({POOL})"

    @app.get("/health")
    @app.get("/healthz")
    async def health():
        return {"status": "ok", "pool": POOL, "model": args.model or "auto",
                "backend": "fake" if args.fake else "laya"}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [
            {"id": args.model or "jev", "object": "model",
             "owned_by": POOL}]}

    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse(generate_latest(registry).decode(),
                                 media_type="text/plain; version=0.0.4")

    @app.post("/v1/systemone")
    async def systemone(request: Request, body: dict = Body(...)):
        state = body.get("state")
        questions = body.get("questions")
        if not isinstance(state, (str, dict, list)) or not isinstance(questions, dict) or not questions:
            return JSONResponse(status_code=400, content={
                "error": {"message": "expected {state, questions: {name: "
                                     "{type: 'noul', instructions: ...}}}",
                          "type": "unsupported_request"}})
        text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        if not text.strip():
            return JSONResponse(status_code=400, content={
                "error": {"message": "state is empty", "type": "unsupported_request"}})
        for name, spec in questions.items():
            if not isinstance(spec, dict) or spec.get("type") != "noul" \
                    or not isinstance(spec.get("instructions"), str):
                return JSONResponse(status_code=400, content={
                    "error": {"message": f"question {name!r} must be a noul "
                                         "with string instructions",
                              "type": "unsupported_question"}})

        started = time.monotonic()
        inflight.labels(pool=POOL).inc()
        try:
            async with semaphore:
                results = await asyncio.to_thread(
                    _answer_all, text, questions, args.model, args.fake)
        except Exception as error:  # noqa: BLE001 - surface as a served 503
            requests_total.labels(pool=POOL, status="error").inc()
            return JSONResponse(status_code=503, content={
                "error": {"message": str(error)[:300], "type": "model_unavailable"}})
        finally:
            inflight.labels(pool=POOL).dec()
            latency.labels(pool=POOL).observe(time.monotonic() - started)

        busy_ms = round((time.monotonic() - started) * 1000, 2)
        device_ms.labels(pool=POOL).inc(busy_ms)
        questions_total.labels(pool=POOL).inc(len(results))
        requests_total.labels(pool=POOL, status="ok").inc()
        return JSONResponse(
            content={"answers": results},
            headers={"X-Busy-Ms": str(busy_ms),
                     "X-Prompt-Tokens": str(max(1, len(text) // 4)),
                     "X-Jev-Model": args.model or "auto"},
        )

    return app


def _fake_score(text: str, instructions: str) -> float:
    """Deterministic stand-in for the fake compose profile only.

    It is intentionally dumb: it answers the PII question with the demo regex
    and the cause question with the measured cause regex, so the routing path
    can be exercised without downloading a checkpoint. It is never a
    measurement and the cluster manifest does not pass --fake.
    """
    lowered = instructions.lower()
    if "terminate" in lowered or "cause" in lowered:
        from ..specialist.questions import CAUSE_TERMINATION_RE

        return 0.95 if CAUSE_TERMINATION_RE.search(text) else 0.05
    return 0.95 if pii.regex_detect(text) else 0.05


def _answer_all(text: str, questions: dict, model: str | None,
                fake: bool = False) -> dict:
    """Run each noul question, serialised because Laya's router is stateful."""
    results: dict = {}
    if fake:
        for name, spec in questions.items():
            score = _fake_score(text, spec["instructions"])
            results[name] = {"type": "noul", "noul": round(score, 6)}
        return results
    with _MODEL_LOCK:
        for name, spec in questions.items():
            score = pii.jev_question_score(
                text, spec["instructions"], model=model, question=name)
            results[name] = {"type": "noul", "noul": round(float(score), 6)}
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Jev/Laya System One pool")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", choices=["auto", "english", "multilingual"],
                        default=None, help="Laya checkpoint (default: auto)")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--fake", action="store_true",
                        help="deterministic regex-backed stand-in for the fake "
                             "compose profile; never deploy this")
    parser.add_argument("--warmup", action="store_true",
                        help="load the checkpoint before reporting ready")
    args = parser.parse_args()
    if args.warmup and not args.fake:
        # Force the lazy router/model onto the pod before the readiness probe
        # passes. The call is intentionally tiny; it is not a benchmark. Fake
        # mode has no checkpoint to warm.
        pii.jev_question_score(
            "warmup", "Is this text a warmup request?", model=args.model,
            question="warmup")
    app = create_app(args)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
