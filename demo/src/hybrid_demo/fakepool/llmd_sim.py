"""Adapter in front of ``llm-d-inference-sim``.

The hand-written fake pool exists because the demo needs three behaviours
that a generic simulator did not guarantee in one place: deterministic tiered
answer corruption, device-time headers (``X-Busy-Ms`` etc.), and a pool name
on every response.  Upstream ``llm-d-inference-sim`` already simulates the
hard parts (OpenAI-compatible responses, token streaming, latency, prefix
cache, KV metrics), so the sustainable path is to run it and keep only the
thin contract adapter.

Usage:

    llm-d-inference-sim --model Semantic-Qwen --port 8001 --mode random
    hybrid-pool-sim --pool vllm-cpu --port 8000 \
        --upstream http://127.0.0.1:8001

Point ``VLLM_CPU_UPSTREAM`` (or the relevant pool env) at the adapter.  The
pipeline, trace UI and metrics scraper keep working unchanged.  The response
gains ``X-Pool``/``X-Simulator`` and proxy timings (``X-Proxy-Busy-Ms``,
``X-Proxy-Ttft-Ms``).

**Do not call proxy wall time device time.** The pipeline's cost report sums
``X-Busy-Ms`` as GPU/CPU busy milliseconds; a proxy measurement includes
network and client overhead and would silently move that number.  The adapter
therefore leaves ``X-Busy-Ms`` untouched unless the simulator itself sent it.
For the measured cost arms, keep the deterministic ``hybrid-pool`` fake (it
computes prefill/decode time from the request); for transport/latency/streaming
tests, use this adapter.  Set ``LLMD_SIM_DEVICE_TIME_HEADERS=1`` only if you
have deliberately decided that proxy time is an acceptable stand-in and will
label the run accordingly.

The adapter deliberately does *not* fabricate answer corruptions: if the
simulator's error injection is sufficient, use it; otherwise keep the existing
``hybrid-pool`` for the deterministic tiered-error arms.  Record which one a
run used, because the error model is part of the measurement.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}


def simulator_command(model: str, port: int, *, mode: str = "random",
                      seed: int | None = None, extra: list[str] | None = None) -> list[str]:
    """The upstream simulator's command line.

    Flags are the ones documented for the current image.  Callers can append
    version-specific flags with ``extra``; the point is to stop this repo from
    re-implementing the simulator, not to pin its full CLI.
    """
    command = ["llm-d-inference-sim", "--model", model, "--port", str(port),
               "--mode", mode]
    if seed is not None:
        command += ["--seed", str(seed)]
    return command + list(extra or [])


def create_proxy_app(upstream: str, pool: str, replica: str | None = None,
                     transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    upstream = upstream.rstrip("/")
    replica = replica or os.environ.get("HOSTNAME", "local")
    client = httpx.AsyncClient(timeout=600.0, transport=transport)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await client.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)
    app.title = f"llm-d-inference-sim adapter ({pool})"

    def headers_out(response: httpx.Response, *, ttft_ms: float | None = None,
                    total_ms: float | None = None) -> dict[str, str]:
        headers = {k: v for k, v in response.headers.items()
                   if k.lower() not in HOP_BY_HOP}
        # A proxy can honestly report wall time, not device time.  TTFT is
        # only known for a stream (we buffer the first chunk below); total is
        # only known for a non-streamed body.  Never invent the other one.
        if ttft_ms is not None:
            headers.setdefault("X-Proxy-Ttft-Ms", str(round(ttft_ms, 1)))
        if total_ms is not None:
            headers.setdefault("X-Proxy-Busy-Ms", str(round(total_ms, 1)))
        if os.environ.get("LLMD_SIM_DEVICE_TIME_HEADERS") == "1" and total_ms is not None:
            headers.setdefault("X-Busy-Ms", str(round(total_ms, 1)))
            headers.setdefault("X-Queue-Ms", "0.0")
        headers.setdefault("X-Pool", pool)
        headers.setdefault("X-Replica", replica)
        headers["X-Simulator"] = "llm-d-inference-sim"
        return headers

    @app.get("/health")
    @app.get("/healthz")
    async def health() -> Response:
        try:
            response = await client.get(upstream + "/health", timeout=5.0)
            return Response(response.content, status_code=response.status_code,
                            media_type=response.headers.get("content-type", "application/json"))
        except httpx.HTTPError as error:
            return JSONResponse(status_code=503, content={"status": "unavailable",
                                                          "error": str(error), "pool": pool})

    @app.get("/metrics")
    async def metrics() -> Response:
        try:
            response = await client.get(upstream + "/metrics", timeout=5.0)
            return PlainTextResponse(response.text, status_code=response.status_code)
        except httpx.HTTPError as error:
            return PlainTextResponse(f"# simulator unavailable: {error}\n", status_code=503)

    @app.get("/v1/models")
    async def models() -> Response:
        response = await client.get(upstream + "/v1/models")
        return Response(response.content, status_code=response.status_code,
                        media_type=response.headers.get("content-type", "application/json"))

    @app.post("/v1/chat/completions")
    @app.post("/v1/completions")
    async def completions(request: Request) -> Response:
        body = await request.body()
        try:
            wants_stream = bool(json.loads(body).get("stream"))
        except (json.JSONDecodeError, AttributeError, TypeError, UnicodeDecodeError):
            wants_stream = False
        streaming = (request.headers.get("accept", "").startswith("text/event-stream")
                     or wants_stream)
        started = time.monotonic()
        try:
            upstream_request = client.build_request(
                request.method, upstream + request.url.path, content=body,
                headers={k: v for k, v in request.headers.items()
                         if k.lower() not in HOP_BY_HOP and k.lower() != "host"},
            )
            response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as error:
            return JSONResponse(status_code=502, content={
                "error": {"message": f"simulator unavailable: {error}", "type": "upstream_error"}})
        if streaming:
            chunks = response.aiter_bytes()
            try:
                first = await anext(chunks)
            except StopAsyncIteration:
                first = b""
            ttft_ms = (time.monotonic() - started) * 1000.0
            headers = headers_out(response, ttft_ms=ttft_ms)

            async def relay():
                try:
                    if first:
                        yield first
                    async for chunk in chunks:
                        yield chunk
                finally:
                    await response.aclose()
            return StreamingResponse(relay(), status_code=response.status_code,
                                     headers=headers)
        content = await response.aread()
        await response.aclose()
        total_ms = (time.monotonic() - started) * 1000.0
        headers = headers_out(response, total_ms=total_ms)
        return Response(content, status_code=response.status_code,
                        headers=headers,
                        media_type=response.headers.get("content-type", "application/json"))

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="adapter for llm-d-inference-sim")
    parser.add_argument("--upstream", required=True, help="simulator base URL")
    parser.add_argument("--pool", required=True, help="routing tier name, e.g. vllm-cpu")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--replica", default=None)
    parser.add_argument("--print-simulator-cmd", action="store_true",
                        help="print a suggested simulator command and exit")
    parser.add_argument("--model", default="Semantic-Qwen")
    args = parser.parse_args()
    if args.print_simulator_cmd:
        print(json.dumps(simulator_command(args.model, 8001)))
        return
    app = create_proxy_app(args.upstream, args.pool, args.replica)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
