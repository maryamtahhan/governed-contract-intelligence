from __future__ import annotations

import httpx

from hybrid_demo import bench
from hybrid_demo.evalkit import rag
from hybrid_demo.fakepool.llmd_sim import create_proxy_app, simulator_command
from hybrid_demo.pipeline.batch import BatchGateway, requests_jsonl
from hybrid_demo.specialist.cache import create_cache


def test_simulator_command_and_app():
    command = simulator_command("Semantic-Qwen", 8001, seed=7)
    assert command[:6] == ["llm-d-inference-sim", "--model", "Semantic-Qwen",
                           "--port", "8001", "--mode"]
    app = create_proxy_app("http://127.0.0.1:9", "vllm-cpu", "r1")
    paths = {route.path for route in app.routes}
    assert "/v1/chat/completions" in paths
    assert "/metrics" in paths


def test_proxy_reports_proxy_time_but_not_device_time(monkeypatch):
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]},
                              request=request)

    monkeypatch.delenv("LLMD_SIM_DEVICE_TIME_HEADERS", raising=False)
    app = create_proxy_app("http://sim", "vllm-cpu", "r1",
                           transport=httpx.MockTransport(handler))

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            response = await client.post("/v1/chat/completions",
                                         json={"model": "m", "messages": []})
            assert response.status_code == 200
            assert response.headers["x-pool"] == "vllm-cpu"
            assert response.headers["x-simulator"] == "llm-d-inference-sim"
            assert "x-proxy-busy-ms" in response.headers
            assert "x-busy-ms" not in response.headers

            streamed = await client.post(
                "/v1/chat/completions",
                json={"model": "m", "messages": [], "stream": True})
            assert "x-proxy-ttft-ms" in streamed.headers
            assert "x-proxy-busy-ms" not in streamed.headers
            assert "x-busy-ms" not in streamed.headers

    asyncio.run(run())


def test_batch_jsonl_and_mock_roundtrip():
    payload = requests_jsonl([{"custom_id": "a", "body": {"model": "m"}}])
    assert '"custom_id": "a"' in payload
    assert '"url": "/v1/chat/completions"' in payload

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/files":
            return httpx.Response(200, json={"id": "file-1"})
        if request.url.path == "/v1/batches":
            return httpx.Response(200, json={"id": "batch-1", "status": "completed",
                                             "output_file_id": "out-1"})
        if request.url.path == "/v1/files/out-1/content":
            return httpx.Response(200, content=b'{"custom_id": "a", "ok": true}\n')
        return httpx.Response(404)

    async def run():
        gateway = BatchGateway("http://batch.test", transport=httpx.MockTransport(handler))
        try:
            batch = await gateway.submit([{"custom_id": "a", "body": {"model": "m"}}])
            assert batch["id"] == "batch-1"
            results = await gateway.results(batch)
            assert results == [{"custom_id": "a", "ok": True}]
        finally:
            await gateway.close()

    import asyncio

    asyncio.run(run())


def test_bench_commands_for_both_backends():
    vllm = bench.build_command("vllm", "http://pool", "m", num_prompts=10,
                               request_rate=2.0, max_concurrency=4,
                               output_json="/tmp/out.json")
    assert vllm[:3] == ["vllm", "bench", "serve"]
    assert "--num-prompts" in vllm and "10" in vllm
    assert "--save-result" in vllm and "--result-filename" in vllm
    assert "--output-json" not in vllm
    guidellm = bench.build_command("guidellm", "http://pool", "m", rate=2.0,
                                   max_seconds=30)
    assert guidellm[:2] == ["guidellm", "run"]
    assert "kind=openai_http,target=http://pool,request_format=/v1/chat/completions" in guidellm
    assert "kind=max_duration,seconds=30" in guidellm


def test_lexical_groundedness_and_context_precision():
    contexts = ["Governing law: the laws of the State of New York.",
                "Payment terms: net thirty days."]
    result = rag.evaluate(
        "The contract is governed by New York law. Payment is net thirty days.",
        contexts,
    )
    assert result["groundedness"] > 0.5
    assert result["context_precision"] == 1.0


def test_groundedness_flags_fabrication():
    result = rag.evaluate("The moon is made of cheese.",
                          ["Governing law: New York."])
    assert result["groundedness"] < 0.5


def test_evaluate_records_uses_extracted_evidence():
    records = [
        {"doc_id": "d1", "step": "extract:termination", "outcome": "served",
         "content": {"found": True, "text": "Either party may terminate"}},
        {"doc_id": "d1", "step": "risk", "outcome": "served",
         "content": {"risk": "low", "reasons": ["Either party may terminate"]}},
    ]
    result = rag.evaluate_records(records)
    assert result["samples"] == 1
    assert result["groundedness"] > 0.5


def test_cache_factory_defaults_to_memory_and_rejects_unknown():
    cache = create_cache("memory")
    assert cache.snapshot()["backend"] == "memory"
    try:
        create_cache("bogus")
    except ValueError as error:
        assert "bogus" in str(error)
    else:  # pragma: no cover
        raise AssertionError("unknown backend should fail")
