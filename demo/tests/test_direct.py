from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from hybrid_demo.pipeline import direct

DATA = Path(__file__).parent / "data-e2e"
# no injected errors: the direct arm has no gateway to fail over to, so an
# error would fail the run rather than the assertion under test
NO_ERRORS = "simple=0,medium=0,complex=0,reasoning=0"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def endpoint():
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "hybrid_demo.fakepool.server",
            "--pool", "vllm-cpu", "--model", "Semantic-Qwen",
            "--host", "127.0.0.1", "--port", str(port),
            "--time-scale", "0.02", "--error-rates", NO_ERRORS,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2.0)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.terminate()
        raise TimeoutError(f"fake pool not ready on port {port}")
    yield f"http://127.0.0.1:{port}/v1"
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_direct_arm_sends_report_before_closing_the_client(endpoint, tmp_path):
    """The report request runs inside the `async with` block.

    Regression: it used to be issued after the client context exited, which
    raised RuntimeError("Cannot send a request, as the client has been
    closed") and lost the whole direct-arm run at its last step.
    """
    out = tmp_path / "out"
    scored = asyncio.run(direct.run(endpoint, str(DATA), str(out),
                                    concurrency=2, timeout=60, docs_limit=2))

    results = json.loads((out / "results.json").read_text())
    report = [r for r in results["records"] if r["step"] == "report"]
    assert len(report) == 1
    assert report[0]["outcome"] == "served"
    assert report[0]["content"], "report answer was not parsed"
    assert set(scored) == {"extraction", "risk_accuracy",
                           "report_accuracy", "docs_fully_correct"}


def test_direct_arm_extracts_the_clause_keys_the_manifest_names(endpoint, tmp_path):
    """The workload comes from the dataset, not from generate.CLAUSE_KEYS.

    Regression: the CUAD test manifest carries audit_rights, but the arm asked
    for the data_protection default. That category has no truth in CUAD, so its
    fields scored correct whenever the model said "not found", and audit_rights
    was never measured at all.
    """
    data = tmp_path / "data"
    (data / "documents").mkdir(parents=True)
    (data / "documents" / "d1.txt").write_text(
        "MASTER SERVICES AGREEMENT. Governed by the laws of New York.")
    (data / "ground_truth.json").write_text(json.dumps({
        "clause_keys": ["governing_law"],
        "documents": [{"doc_id": "d1",
                       "clauses": {"governing_law": "laws of New York"}}],
    }))
    out = tmp_path / "out"
    asyncio.run(direct.run(endpoint, str(data), str(out),
                           concurrency=1, timeout=60, docs_limit=None))

    results = json.loads((out / "results.json").read_text())
    steps = [r["step"] for r in results["records"] if r["step"].startswith("extract")]
    assert steps == ["extract:governing_law"]


def test_report_lines_are_one_per_document_and_fit_the_context():
    """The report input must not grow with the number of extraction records.

    Regression: it used to be one line per extraction record. At 200 documents
    that is ~150k chars (~37k tokens) against a 16k max-model-len, so the run
    failed with HTTP 400 at the report step *after* all per-document work had
    been paid for (2026-11-04).
    """
    doc_ids = [f"C-2026-{i:04d}" for i in range(1, 201)]
    records = []
    for doc_id in doc_ids:
        records.append({"doc_id": doc_id, "step": "classify",
                        "content": {"type": "MSA"}})
        for clause in ("termination", "governing_law", "liability_cap",
                       "data_protection"):
            records.append({"doc_id": doc_id, "step": f"extract:{clause}",
                            "content": {"found": True, "text": "x" * 200}})
        records.append({"doc_id": doc_id, "step": "risk",
                        "content": {"risk": "medium"}})

    lines = direct.report_lines(records, doc_ids)
    assert len(lines) == 200, "one line per document, not per extraction"
    assert all(line.startswith("- {") for line in lines)
    assert sum(len(line) for line in lines) < 45_000


def test_streaming_run_yields_the_same_steps_as_the_parallel_run(endpoint):
    """The SSE path is the parallel path, observed as it lands."""
    text = (DATA / "documents" / "C-2026-0001.txt").read_text()
    clauses = ["termination", "governing_law"]

    async def collect():
        sem = asyncio.Semaphore(8)
        async with httpx.AsyncClient(base_url=endpoint, timeout=60) as client:
            streamed = [rec async for rec in direct.run_doc_stream(
                client, sem, "C-2026-0001", text, clauses)]
            batch = await direct.run_doc(client, sem, "C-2026-0001", text,
                                         clauses, parallel=True)
        return streamed, batch

    streamed, batch = asyncio.run(collect())
    assert (sorted(r["step"] for r in streamed)
            == sorted(r["step"] for r in batch))
    assert all(r["outcome"] == "served" for r in streamed)
    risk = [r for r in streamed if r["step"] == "risk"]
    assert risk and risk[0]["content"].get("risk")
    # risk is last by construction: it reads the extracts.
    assert streamed[-1]["step"] == "risk"
