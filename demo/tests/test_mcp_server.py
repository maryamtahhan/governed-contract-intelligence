from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from hybrid_demo.mcp import server  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "D1.txt").write_text(
        "MASTER SERVICES AGREEMENT\nContact a@b.com about personal data."
    )
    manifest = {
        "documents": [{"doc_id": "D1", "type": "MSA", "sensitive": True}],
        "clause_keys": server.CLAUSE_KEYS,
    }
    (tmp_path / "ground_truth.json").write_text(json.dumps(manifest))
    monkeypatch.setenv("HYBRID_DATA", str(tmp_path))
    monkeypatch.setenv("HYBRID_GATEWAY", "http://gateway")
    monkeypatch.setenv("HYBRID_SENSITIVE_GATEWAY", "http://sensitive")
    monkeypatch.setenv("HYBRID_JUDGMENT_GATEWAY", "http://judgment")
    monkeypatch.setenv("HYBRID_SENSITIVE_JUDGMENT_GATEWAY", "http://sensitive-judgment")
    monkeypatch.delenv("HYBRID_PII_DETECTOR", raising=False)
    return tmp_path


def test_contract_resource_returns_text_and_annotations(data_dir: Path) -> None:
    payload = json.loads(server.contract("D1"))
    assert payload["doc"]["type"] == "MSA"
    assert "a@b.com" in payload["text"]


def test_contract_resource_rejects_unknown_id(data_dir: Path) -> None:
    with pytest.raises(ValueError, match="unknown document"):
        server.contract("missing")


def test_screen_document_uses_the_configured_detector(data_dir: Path) -> None:
    result = server.screen_document("write to a@b.com")
    assert result["sensitive"] is True
    assert result["detector"] == "regex"
    assert result["findings"][0]["entity"] == "EMAIL_ADDRESS"


async def test_review_document_wraps_the_pipeline(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    async def fake_run_arm(**kwargs):
        seen.update(kwargs)
        Path(kwargs["out_dir"], "results.json").write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "step": "classify",
                            "entry": "general",
                            "outcome": "served",
                            "headers": {"x-demo-route": "vllm-cpu"},
                            "response_model": "Semantic-Qwen",
                            "latency_ms": 12,
                            "content": {"type": "MSA"},
                        }
                    ]
                }
            )
        )
        return {"docs": 1, "errors": 0, "sensitive_violations": 0}

    monkeypatch.setattr(server, "run_arm", fake_run_arm)
    result = await server.review_document("D1")

    assert seen["doc_ids"] == ["D1"]
    assert seen["arm"] == "D"
    assert result["summary"]["errors"] == 0
    assert result["stages"]["classify"]["route"] == "vllm-cpu"


async def test_stage_tools_validate_clause_keys(data_dir: Path) -> None:
    with pytest.raises(ValueError, match="unknown clause"):
        await server.extract_clause("D1", "not_a_clause")
    with pytest.raises(ValueError, match="unknown clauses"):
        await server.assess_risk("D1", {"not_a_clause": "text"})
    with pytest.raises(ValueError, match="at least one document"):
        await server.build_report([])


async def test_tool_and_resource_catalog() -> None:
    tools = await server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {
        "review_document",
        "screen_document",
        "extract_clause",
        "assess_risk",
        "build_report",
    } <= names
    assert await server.mcp.list_resource_templates()
