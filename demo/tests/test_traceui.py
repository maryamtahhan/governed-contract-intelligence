from __future__ import annotations

import importlib
import json
import time

import pytest


@pytest.fixture()
def traceui(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_DB_PATH", str(tmp_path / "traces.db"))
    monkeypatch.setenv("TRACE_POOLS", "vllm-cpu,vllm-gpu,vllm-gpu-approved,vllm-gpu-small")
    monkeypatch.setenv("VLLM_CPU_UPSTREAM", "http://cpu:8000")
    monkeypatch.setenv("VLLM_GPU_UPSTREAM", "http://gpu:8000")
    monkeypatch.setenv("VLLM_GPU_APPROVED_UPSTREAM", "http://approved:8000")
    monkeypatch.setenv("VLLM_GPU_SMALL_UPSTREAM", "http://small:8000")
    monkeypatch.setenv("VLLM_CPU_RELAY_PORT", "18081")
    monkeypatch.setenv("VLLM_GPU_APPROVED_RELAY_PORT", "18084")
    monkeypatch.delenv("SENSITIVE_UPSTREAM", raising=False)
    module = importlib.import_module("hybrid_demo.traceui.server")
    return importlib.reload(module)


def test_pool_parsing(traceui):
    assert set(traceui.POOLS) == {"vllm-cpu", "vllm-gpu", "vllm-gpu-approved", "vllm-gpu-small"}
    assert traceui.POOLS["vllm-cpu"][0] == "http://cpu:8000"
    assert traceui.POOLS["vllm-cpu"][1] == 18081
    assert traceui.POOLS["vllm-gpu-approved"][1] == 18084
    assert traceui.POOLS["vllm-gpu"][1] == 8082


def test_trace_roundtrip_and_summary(traceui):
    traceui.create_trace("t1", "POST", "/v1/chat/completions", "r1", "question?", "sensitive")
    traceui.record_hop("r1", "vllm-gpu-approved", "REASONING")
    traceui.record_hop_timing("t1", "vllm-gpu-approved", time.time(), 0.0, 120, 200, "150")
    traceui.complete_trace(
        "t1", 200,
        {"x-demo-classification": "REASONING", "x-demo-route": "vllm-gpu-approved"},
        started=0.0,
        response=('{"documents": 3}', 3, False),
    )
    rows = traceui.traces()
    assert len(rows) == 1
    row = rows[0]
    assert row["entry"] == "sensitive"
    assert row["classification"] == "REASONING"
    assert row["route"] == "vllm-gpu-approved"
    assert row["fallback"] == 0
    assert row["response"] == '{"documents": 3}'
    assert row["hops"][0]["route"] == "vllm-gpu-approved"

    summary = traceui.trace_summary()
    assert summary["total"] == 1
    group = summary["groups"][0]
    assert group["entry"] == "sensitive"
    assert group["route"] == "vllm-gpu-approved"
    assert group["count"] == 1


def test_fallback_detection_from_mismatch(traceui):
    classification, route, fallback = traceui.routing_signal(
        {"x-demo-classification": "REASONING", "x-demo-route": "vllm-cpu"}
    )
    assert (classification, route, fallback) == ("REASONING", "vllm-cpu", True)
    _c, _r, fallback2 = traceui.routing_signal(
        {"x-demo-classification": "SIMPLE", "x-demo-route": "vllm-cpu", "x-praxis-fallback": "true"}
    )
    assert fallback2 is True


def test_runs_and_audit_export(traceui):
    traceui.record_run({"arm": "D", "docs": 12, "sensitive_violations": 0, "routes": {"vllm-cpu": 5}})
    traceui.record_run({"arm": "D", "docs": 6, "sensitive_violations": 0, "routes": {}})
    traceui.record_run({"arm": "A", "docs": 12, "sensitive_violations": 3, "routes": {}})
    runs = traceui.list_runs()
    assert [r["arm"] for r in runs] == ["A", "D"]
    assert runs[1]["docs"] == 6

    traceui.create_trace("t2", "POST", "/v1/chat/completions", "r2", None, "general")
    traceui.complete_trace("t2", 200, {"x-demo-route": "vllm-gpu"}, 0.0)
    audit = traceui.audit_jsonl()
    assert json.loads(audit.splitlines()[0])["entry"] == "general"


def test_sensitive_ingress_flag(traceui, monkeypatch):
    monkeypatch.setenv("SENSITIVE_UPSTREAM", "http://praxis:8083")
    monkeypatch.setenv("SENSITIVE_INGRESS_PORT", "8083")
    module = importlib.reload(traceui)
    assert module.SENSITIVE_UPSTREAM == "http://praxis:8083"
    assert module.SENSITIVE_INGRESS_PORT == 8083
