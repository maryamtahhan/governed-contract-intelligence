from __future__ import annotations

import json

from hybrid_demo.pipeline import schemas
from hybrid_demo.traceui import otel


def test_structured_outputs_are_off_by_default(monkeypatch):
    monkeypatch.delenv("HYBRID_STRUCTURED_OUTPUTS", raising=False)
    body = {"model": "m"}
    assert schemas.apply(body, "classify") == body
    assert schemas.mode() == "off"


def test_vllm_structured_outputs_wire_format(monkeypatch):
    monkeypatch.setenv("HYBRID_STRUCTURED_OUTPUTS", "vllm")
    body = schemas.apply({"model": "m"}, "extract")
    assert body["structured_outputs"]["json"]["required"] == [
        "clause", "found", "text", "ref"]
    assert schemas.mode() == "vllm"


def test_legacy_and_openai_wire_formats(monkeypatch):
    monkeypatch.setenv("HYBRID_STRUCTURED_OUTPUTS", "legacy")
    assert "guided_json" in schemas.apply({}, "risk")
    monkeypatch.setenv("HYBRID_STRUCTURED_OUTPUTS", "openai")
    body = schemas.apply({}, "report")
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "report_answer"


def test_review_bodies_pick_up_schema(monkeypatch):
    from hybrid_demo.pipeline.review import classify_body

    monkeypatch.delenv("HYBRID_STRUCTURED_OUTPUTS", raising=False)
    assert "structured_outputs" not in classify_body("contract")
    monkeypatch.setenv("HYBRID_STRUCTURED_OUTPUTS", "vllm")
    assert "structured_outputs" in classify_body("contract")


def test_otel_jsonl_export_shape(tmp_path, monkeypatch):
    path = tmp_path / "spans.jsonl"
    monkeypatch.setenv("TRACE_OTEL", "jsonl")
    monkeypatch.setenv("TRACE_OTEL_JSONL", str(path))
    row = {
        "id": "t1", "started_at": 100.0, "completed_at": 100.5,
        "method": "POST", "path": "/v1/chat/completions", "status": 200,
        "classification": "SIMPLE", "route": "vllm-cpu", "fallback": 0,
        "duration_ms": 500, "response_headers": {"x-prompt-tokens": "12"},
        "entry": "general", "question": "hello", "response": "world",
        "response_tokens": 3, "response_tokens_est": False, "error": "",
    }
    span = otel.emit_trace(row, [{"route": "vllm-cpu", "arrived_at": 100.1,
                                  "ttfb_ms": 50, "total_ms": 200,
                                  "status": 200, "classifier_us": 10,
                                  "error": ""}])
    assert span is not None
    assert span["attributes"]["gen_ai.usage.input_tokens"] == 12
    assert span["attributes"]["app.routing.route"] == "vllm-cpu"
    assert span["events"][0]["name"] == "praxis.hop"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["trace_id"] == "t1"


def test_otel_sensitive_rows_do_not_export_text(tmp_path, monkeypatch):
    path = tmp_path / "spans.jsonl"
    monkeypatch.setenv("TRACE_OTEL", "jsonl")
    monkeypatch.setenv("TRACE_OTEL_JSONL", str(path))
    row = {
        "id": "t2", "started_at": 1.0, "completed_at": 2.0, "method": "POST",
        "path": "/v1/chat/completions", "status": 200, "classification": "REASONING",
        "route": "vllm-gpu-approved", "fallback": 0, "duration_ms": 1000,
        "response_headers": {}, "entry": "sensitive", "question": None,
        "response": "redacted", "response_tokens": 4, "response_tokens_est": False,
        "error": "",
    }
    otel.emit_trace(row, [])
    persisted = path.read_text()
    assert "question" not in persisted
    assert "hello" not in persisted


def test_otel_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACE_OTEL", raising=False)
    assert otel.emit_trace({"id": "t3"}) is None
    assert list(tmp_path.iterdir()) == []
