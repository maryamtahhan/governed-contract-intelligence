from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hybrid_demo.evalkit.score import build_report, governance_check
from hybrid_demo.pipeline.generate import generate
from hybrid_demo.pipeline.review import run_arm

DATA = Path(__file__).parent / "data-e2e"
APPROVED = {"vllm-cpu", "vllm-gpu-approved"}

REASONING_PROMPT = (
    "Produce the cross-document risk report across all 3 documents. Synthesise and compare the "
    "per-document extractions, derive the aggregate counts, and prove the ranking. Return JSON."
)
SIMPLE_PROMPT = "What is the capital of France?"


@pytest.fixture(scope="module", autouse=True)
def dataset():
    if not (DATA / "ground_truth.json").exists():
        generate(DATA, count=200, seed=42, golden=12)
    return DATA


def routes(records: list[dict]) -> set:
    return {r["headers"].get("x-demo-route") for r in records}


async def run(stack, arm: str, out: str, docs: int = 12) -> dict:
    return await run_arm(
        gateway_base=stack.gateway,
        sensitive_base=stack.sensitive_gateway,
        data_dir=str(DATA),
        out_dir=out,
        arm=arm,
        docs_limit=docs,
        concurrency=12,
    )


async def send(stack, entry: str, prompt: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=120) as client:
        return await client.post(
            f"{stack.gateway if entry == 'general' else stack.sensitive_gateway}/v1/chat/completions",
            json={"model": "Semantic-Qwen", "temperature": 0, "max_tokens": 48,
                  "messages": [{"role": "user", "content": prompt}]},
        )


@pytest.mark.asyncio
async def test_arm_d_full_matrix(stack, tmp_path):
    stack.start_stub("config.yaml")
    summary = await run(stack, "D", str(tmp_path / "d"))
    assert summary["errors"] == 0
    assert summary["sensitive_violations"] == 0
    assert summary["screen"]["tp"] > 0

    results = json.loads((tmp_path / "d" / "results.json").read_text())
    records = results["records"]

    per_clause = [r for r in records if r["step"].startswith("extract:") or r["step"] == "classify"]
    assert per_clause
    assert routes(per_clause) == {"vllm-cpu"}

    risk_steps = [r for r in records if r["step"] == "risk"]
    assert routes(risk_steps) == {"vllm-cpu"}

    report_steps = [r for r in records if r["step"] == "report"]
    assert report_steps and report_steps[0]["headers"]["x-demo-route"] == "vllm-gpu-approved"

    sensitive = [r for r in records if r["true_sensitive"]]
    assert sensitive
    assert all(r["entry"] == "sensitive" for r in sensitive)
    assert all(r["headers"].get("x-demo-route") in APPROVED for r in sensitive)

    gov = governance_check(stack.gateway)
    assert gov["clean"] is True
    assert gov["sensitive_entry_traces"] > 0
    assert "vllm-gpu-approved" in gov["routes"]

    runs = httpx.get(f"{stack.gateway}/_trace/api/runs", timeout=10).json()["runs"]
    assert any(r["arm"] == "D" for r in runs)


@pytest.mark.asyncio
async def test_pull_the_plug_fallback_and_recovery(stack, tmp_path):
    stack.start_stub("config.yaml")

    resp = await send(stack, "general", REASONING_PROMPT)
    assert resp.status_code == 200
    assert resp.headers["x-demo-route"] == "vllm-gpu"
    assert "x-praxis-fallback" not in resp.headers

    stack.kill_pool("vllm-gpu")
    resp = await send(stack, "general", REASONING_PROMPT)
    assert resp.status_code == 200
    assert resp.headers["x-demo-route"] == "vllm-cpu"
    assert resp.headers.get("x-praxis-fallback") == "true"

    resp = await send(stack, "general", SIMPLE_PROMPT)
    assert resp.status_code == 200
    assert resp.headers["x-demo-route"] == "vllm-cpu"
    assert "x-praxis-fallback" not in resp.headers

    summary = await run(stack, "D", str(tmp_path / "plug"), docs=6)
    assert summary["errors"] == 0

    stack.start_pool("vllm-gpu")
    resp = await send(stack, "general", REASONING_PROMPT)
    assert resp.status_code == 200
    assert resp.headers["x-demo-route"] == "vllm-gpu"
    assert "x-praxis-fallback" not in resp.headers

    recovered = await run(stack, "D", str(tmp_path / "recovered"), docs=6)
    assert recovered["fallback"] == 0
    assert recovered["errors"] == 0


@pytest.mark.asyncio
async def test_fail_closed_when_approved_pool_down(stack, tmp_path):
    stack.start_stub("config.yaml")

    stack.kill_pool("vllm-gpu-approved")
    resp = await send(stack, "sensitive", REASONING_PROMPT)
    assert resp.status_code != 200
    assert resp.headers.get("x-demo-route") in (None, "vllm-gpu-approved")

    resp = await send(stack, "sensitive", SIMPLE_PROMPT)
    assert resp.status_code == 200
    assert resp.headers["x-demo-route"] == "vllm-cpu"

    resp = await send(stack, "general", REASONING_PROMPT)
    assert resp.status_code == 200
    assert resp.headers["x-demo-route"] == "vllm-gpu"

    await run(stack, "D", str(tmp_path / "failclosed"), docs=8)
    results = json.loads((tmp_path / "failclosed" / "results.json").read_text())
    reports = [r for r in results["records"] if r["step"] == "report"]
    assert reports
    for r in reports:
        assert r["outcome"] == "http_503"
        assert r["headers"].get("x-demo-route") in (None, "vllm-gpu-approved")
    served = [r for r in results["records"] if r["outcome"] == "served"]
    assert served

    gov = governance_check(stack.gateway)
    assert gov["clean"] is True


@pytest.mark.asyncio
async def test_arm_a_all_gpu(stack, tmp_path):
    stack.start_stub("arm-a.yaml")
    summary = await run(stack, "A", str(tmp_path / "a"))
    assert set(summary["routes"]) == {"vllm-gpu"}
    assert summary["sensitive_steps"] > 0
    assert summary["sensitive_violations"] > 0


@pytest.mark.asyncio
async def test_arm_b_gpu_split(stack, tmp_path):
    stack.start_stub("arm-b.yaml")
    summary = await run(stack, "B", str(tmp_path / "b"))
    assert "vllm-gpu-small" in summary["routes"]
    assert "vllm-gpu" in summary["routes"]
    results = json.loads((tmp_path / "b" / "results.json").read_text())
    per_clause = [r for r in results["records"]
                  if r["step"].startswith("extract:") or r["step"] == "classify"]
    assert routes(per_clause) == {"vllm-gpu-small"}


@pytest.mark.asyncio
async def test_arm_c_round_robin_ungoverned(stack, tmp_path):
    stack.start_stub("arm-c.yaml")
    summary = await run(stack, "C", str(tmp_path / "c"))
    assert set(summary["routes"]) == {"vllm-cpu", "vllm-gpu"}
    assert summary["sensitive_violations"] > 0
    assert set(summary["entries"]) == {"general"}


@pytest.mark.asyncio
async def test_report_covers_all_arms(stack, tmp_path):
    for arm, cfg in (("A", "arm-a.yaml"), ("B", "arm-b.yaml"), ("C", "arm-c.yaml"), ("D", "config.yaml")):
        stack.start_stub(cfg)
        await run(stack, arm, str(tmp_path / f"r-{arm}"), docs=8)
    text, data = build_report(
        [str(tmp_path / f"r-{a}") for a in "ABCD"], str(DATA), cores_per_cpu_replica=8
    )
    assert "Quality vs ground truth" in text
    assert "Routing and governance" in text
    by_arm = {s["arm"]: s for s in data["arms"]}
    assert by_arm["D"]["routing"]["sensitive_violations"] == 0
    assert by_arm["C"]["routing"]["sensitive_violations"] > 0
    assert by_arm["A"]["routing"]["cpu_served_share"] == 0
    assert by_arm["A"]["routing"]["gpu_seconds_per_doc"] > by_arm["D"]["routing"]["gpu_seconds_per_doc"]
    assert by_arm["D"]["extraction"]["f1"] > 0.7
    assert by_arm["A"]["extraction"]["f1"] >= by_arm["D"]["extraction"]["f1"] - 0.15
