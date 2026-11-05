"""Client for the OpenAI-compatible batch API (llm-d-batch-gateway).

The demo's Python runner is the measurement harness: it needs per-document
records, routing headers and arm comparisons, which a batch API does not give
it.  The batch gateway is therefore not a replacement for the runner; it is
the transport for the "portfolio at scale" story:

    runner (build requests) -> /v1/files + /v1/batches -> praxis -> pools
                             <- /v1/files/{output} <- results

``llm-d/llm-d-batch-gateway`` implements ``/v1/batches`` and ``/v1/files``
with 50k-request jobs, progress tracking and adaptive dispatch.  Point
``--gateway`` at the trace-ui/praxis ingress (not directly at a pool), so the
batch path keeps the same governance and audit as interactive work.

The client is intentionally small and dependency-light (httpx only); it can
also talk to any other OpenAI-compatible batch implementation for testing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

DEFAULT_ENDPOINT = "/v1/chat/completions"


class BatchError(RuntimeError):
    pass


def batch_line(custom_id: str, body: dict[str, Any],
               endpoint: str = DEFAULT_ENDPOINT) -> dict[str, Any]:
    """One line of the JSONL input file."""
    return {"custom_id": custom_id, "method": "POST", "url": endpoint, "body": body}


def requests_jsonl(requests: Iterable[dict[str, Any]],
                   endpoint: str = DEFAULT_ENDPOINT) -> str:
    lines = []
    for request in requests:
        if "body" in request:
            line = {**request, "method": request.get("method", "POST"),
                    "url": request.get("url", endpoint)}
        else:
            line = batch_line(str(request["custom_id"]), request["request"], endpoint)
        lines.append(json.dumps(line, ensure_ascii=False))
    return "\n".join(lines) + "\n"


class BatchGateway:
    def __init__(self, base_url: str, token: str | None = None,
                 timeout: float = 120.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                        timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self.client.aclose()

    async def _json(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise BatchError(f"{response.status_code}: {response.text[:500]}")
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}

    async def upload_file(self, filename: str, content: str | bytes) -> dict[str, Any]:
        if isinstance(content, str):
            content = content.encode()
        response = await self.client.post(
            "/v1/files", files={"file": (filename, content, "application/jsonl")},
            data={"purpose": "batch"},
        )
        return await self._json(response)

    async def create_batch(self, input_file_id: str, endpoint: str = DEFAULT_ENDPOINT,
                           completion_window: str = "24h",
                           metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"input_file_id": input_file_id, "endpoint": endpoint,
                   "completion_window": completion_window}
        if metadata:
            payload["metadata"] = metadata
        response = await self.client.post("/v1/batches", json=payload)
        return await self._json(response)

    async def get_batch(self, batch_id: str) -> dict[str, Any]:
        return await self._json(await self.client.get(f"/v1/batches/{batch_id}"))

    async def download_file(self, file_id: str) -> bytes:
        response = await self.client.get(f"/v1/files/{file_id}/content")
        if response.status_code >= 400:
            raise BatchError(f"{response.status_code}: {response.text[:500]}")
        return response.content

    async def wait(self, batch_id: str, poll: float = 2.0,
                   timeout: float = 3600.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            batch = await self.get_batch(batch_id)
            if batch.get("status") in {"completed", "failed", "expired", "cancelled"}:
                return batch
            if time.monotonic() > deadline:
                raise TimeoutError(f"batch {batch_id} still {batch.get('status')!r}")
            await asyncio.sleep(poll)

    async def results(self, batch: dict[str, Any] | str) -> list[dict[str, Any]]:
        if isinstance(batch, str):
            batch = await self.wait(batch)
        if batch.get("status") != "completed":
            raise BatchError(f"batch status {batch.get('status')!r}: "
                             f"{batch.get('errors') or batch.get('error')}")
        file_id = batch.get("output_file_id")
        if not file_id:
            return []
        raw = await self.download_file(file_id)
        return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]

    async def submit(self, requests: Iterable[dict[str, Any]],
                     filename: str = "hybrid-requests.jsonl",
                     endpoint: str = DEFAULT_ENDPOINT,
                     metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = requests_jsonl(requests, endpoint)
        uploaded = await self.upload_file(filename, payload)
        return await self.create_batch(uploaded["id"], endpoint=endpoint, metadata=metadata)

    async def run(self, requests: Iterable[dict[str, Any]],
                  filename: str = "hybrid-requests.jsonl",
                  poll: float = 2.0, timeout: float = 3600.0) -> list[dict[str, Any]]:
        batch = await self.submit(requests, filename=filename)
        return await self.results(await self.wait(batch["id"], poll=poll, timeout=timeout))


def load_requests(path: str | Path) -> list[dict[str, Any]]:
    requests = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            item = json.loads(line)
            if "body" in item:
                requests.append(item)
            elif "request" in item:
                requests.append({"custom_id": item["custom_id"],
                                 "body": item["request"]})
            else:
                raise SystemExit(f"{path}: line needs 'body' or 'request': {line[:80]}")
    return requests


def main() -> None:
    parser = argparse.ArgumentParser(description="submit a batch through praxis")
    parser.add_argument("--gateway", required=True,
                        help="trace-ui/praxis ingress base URL (do not bypass governance)")
    parser.add_argument("--requests", required=True, help="JSONL of {custom_id, body}")
    parser.add_argument("--out", default=None, help="write result JSONL here")
    parser.add_argument("--token", default=None)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--poll", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=3600.0)
    args = parser.parse_args()

    async def run() -> None:
        gateway = BatchGateway(args.gateway, token=args.token)
        try:
            results = await gateway.run(load_requests(args.requests),
                                        poll=args.poll, timeout=args.timeout)
        finally:
            await gateway.close()
        text = "\n".join(json.dumps(row, ensure_ascii=False) for row in results)
        if args.out:
            Path(args.out).write_text(text + ("\n" if text else ""))
        print(text)

    asyncio.run(run())


if __name__ == "__main__":
    main()
