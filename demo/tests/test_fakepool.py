from __future__ import annotations

import json

from hybrid_demo.fakepool.server import (
    detect_task,
    risk_from_clauses,
    simulate_answer,
)

DOC = """MASTER SERVICES AGREEMENT
Contract Reference: C-2026-0007
Effective Date: 2026-02-14   Initial Term: 24 months

CLAUSES
Termination: Either party may terminate this Agreement upon ninety (90) days' prior written notice.
Governing Law: This Agreement shall be governed by the laws of Ireland.
Liability Cap: The Supplier's aggregate liability shall not exceed EUR 1,000,000 in the aggregate.
(No data-protection clause is present in this Agreement.)
"""

NO_ERRORS = {"simple": 0.0, "medium": 0.0, "complex": 0.0, "reasoning": 0.0}


def extraction_prompt(clause: str) -> str:
    return (
        f"Extract the {clause} clause from the following contract. If the clause is absent, "
        f"say not found.\n\n{DOC}\n\nReturn JSON: {{...}}"
    )


def test_task_detection():
    assert detect_task(extraction_prompt("termination")) == "clause_extraction"
    assert detect_task("Please classify the following contract by type.\n" + DOC) == "document_classification"
    risk = "Assess the overall risk of this contract given the extracted clauses below."
    assert detect_task(risk) == "risk_assessment"
    assert detect_task("Produce the cross-document risk report across all 12 documents.") == "cross_document_report"
    assert detect_task("hello") == "generic"


def test_extraction_answer():
    content, task = simulate_answer(extraction_prompt("termination"), "m", NO_ERRORS)
    answer = json.loads(content)
    assert task == "clause_extraction"
    assert answer["found"] is True
    assert "ninety (90) days" in answer["text"]
    assert answer["ref"] == "C-2026-0007"


def test_extraction_absent_clause():
    content, _ = simulate_answer(extraction_prompt("data protection"), "m", NO_ERRORS)
    answer = json.loads(content)
    assert answer["found"] is False
    assert answer["text"] is None


def test_risk_rules():
    clauses = {
        "data_protection": {"found": False, "text": None},
        "termination": {"found": True, "text": "either party may terminate"},
        "liability_cap": {"found": True, "text": "capped at fees"},
    }
    assert risk_from_clauses(clauses) == "high"
    clauses["data_protection"] = {"found": True, "text": "complies with law"}
    assert risk_from_clauses(clauses) == "low"
    clauses["termination"]["text"] = "terminated only for material breach uncured"
    assert risk_from_clauses(clauses) == "medium"


def test_report_answer_aggregates():
    lines = "\n".join(
        f'- {json.dumps({"ref": f"C-{i}", "risk": r, "governing_law": "ireland", "data_protection": dp})}'
        for i, (r, dp) in enumerate([("low", True), ("high", False), ("medium", True)])
    )
    prompt = f"cross-document risk report across all 3 documents\n{lines}\nReturn JSON"
    content, task = simulate_answer(prompt, "m", NO_ERRORS)
    assert task == "cross_document_report"
    answer = json.loads(content)
    assert answer["documents"] == 3
    assert answer["risk_counts"] == {"low": 1, "medium": 1, "high": 1}
    assert answer["missing_data_protection"] == 1
    assert answer["highest_risk_ref"] == "C-1"


def test_error_injection_deterministic():
    rates = {"simple": 1.0, "complex": 1.0, "reasoning": 1.0}
    a, _ = simulate_answer(extraction_prompt("termination"), "cpu", rates)
    b, _ = simulate_answer(extraction_prompt("termination"), "cpu", rates)
    assert a == b
    answer = json.loads(a)
    assert answer["found"] is False or answer["ref"] != "C-2026-0007" or answer["text"] is None
