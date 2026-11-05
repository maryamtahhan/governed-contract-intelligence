from __future__ import annotations

import json
from pathlib import Path

from hybrid_demo.ui.payload import _provenance, _routing, build_document

DATA = Path(__file__).parent / "data-e2e"
DOC_ID = "C-2026-0001"


def test_provenance_defaults_to_example_until_rosa():
    """A missing provenance field must never promote a run to measured."""
    assert _provenance({}) == "example"
    assert _provenance({"environment": "g5.12xlarge"}) == "example"
    assert _provenance({"environment": "rosa"}) == "measured"
    assert _provenance({"environment": "pre-rosa"}) == "example"
    assert _provenance({"environment": "ROSA", "provenance": "example"}) == "example"
    assert _provenance({"provenance": "measured"}) == "measured"


def _truth() -> dict:
    manifest = json.loads((DATA / "ground_truth.json").read_text())
    return next(d for d in manifest["documents"] if d["doc_id"] == DOC_ID)


def _doc(records: list[dict], scored: bool = True,
         truth: dict | None = None) -> dict:
    text = (DATA / "documents" / f"{DOC_ID}.txt").read_text()
    return build_document(DOC_ID, text, records, truth or _truth(),
                          ["termination", "governing_law"], scored=scored)


def _rec(step: str, outcome: str = "served", content: dict | None = None) -> dict:
    return {"step": step, "doc_id": DOC_ID, "outcome": outcome,
            "latency_ms": 5.0, "content": content or {}}


def test_failed_call_is_missing_not_a_free_correct_absence():
    """An unreachable endpoint must not earn "correct absent" credit.

    Regression: the verdict looked only at ``found``, so a ConnectError and a
    served "not found" scored the same — a dead endpoint reported 3/5 correct.
    """
    doc = _doc([
        _rec("classify", "error: ConnectError"),
        _rec("extract:termination", "error: ConnectError"),
        _rec("extract:governing_law", "error: ConnectError"),
        _rec("risk", "error: ConnectError"),
    ])

    verdicts = [e["verdict"] for e in doc["extracts"]]
    assert verdicts == ["missing", "missing"]
    assert doc["correct_fields"] == 0
    assert doc["fully_correct"] is False


def test_served_not_found_without_truth_is_correct_absence():
    truth = {"doc_id": DOC_ID, "type": "MSA", "risk": "low",
             "clauses": {"governing_law": "laws of England"}}
    doc = _doc([
        _rec("classify", content={"type": "MSA"}),
        _rec("extract:termination", content={"found": False}),
        _rec("extract:governing_law", content={"found": True,
                                               "text": "laws of England"}),
        _rec("risk", content={"risk": "low", "reasons": []}),
    ], truth=truth)

    assert [e["verdict"] for e in doc["extracts"]] == [
        "correct_absent", "correct"]
    assert doc["correct_fields"] == doc["fields_total"] == 4


def test_submitted_document_claims_no_correctness():
    doc = _doc([_rec("classify", content={"type": "MSA"}),
                _rec("extract:termination"), _rec("extract:governing_law"),
                _rec("risk", content={"risk": "low"})], scored=False)

    assert doc["scored"] is False
    assert {e["verdict"] for e in doc["extracts"]} == {"unscored"}
    assert doc["fields_total"] == 0
    assert doc["fully_correct"] is False
    assert doc["classify"]["match"] is None
    assert doc["risk"]["match"] is None


def test_route_comes_from_the_response_headers():
    rec = _rec("classify", content={"type": "MSA"})
    rec["headers"] = {"x-demo-route": "vllm-cpu",
                      "x-demo-entry": "sensitive",
                      "x-praxis-trace-id": "t-1"}
    doc = _doc([rec])

    assert doc["classify"]["route"] == "vllm-cpu"
    assert doc["classify"]["entry"] == "sensitive"
    assert doc["classify"]["trace_id"] == "t-1"
    assert doc["routes"] == {"vllm-cpu": 1}


def test_a_failed_call_is_not_reported_as_an_unrouted_request():
    """A 504 has no route header because nothing answered.

    Regression: ``_routing`` filled the route with the string "unrouted", so a
    timeout on a sensitive document looked like the gateway had declined to
    route it — the opposite of what happened.
    """
    routed = _rec("extract:termination", content={"found": False})
    routed["headers"] = {"x-demo-route": "vllm-cpu"}
    routed["true_sensitive"] = True
    failed = _rec("extract:governing_law", "http_504")
    failed["headers"] = {"x-praxis-trace-id": "t-2"}
    failed["true_sensitive"] = True

    routing = _routing([routed, failed])

    assert routing["sensitive_routes"] == {"vllm-cpu": 1}
    assert "unrouted" not in routing["sensitive_routes"]
    assert routing["sensitive_no_route"] == 1
    assert routing["no_route"] == 1
    assert routing["no_route_failed"] == 1
