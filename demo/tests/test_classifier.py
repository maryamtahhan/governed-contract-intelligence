from __future__ import annotations

from hybrid_demo.classifier import classify_complexity, detect_sensitivity
from hybrid_demo.pipeline.review import classify_body, extract_body, report_body, risk_body

DOC = """MASTER SERVICES AGREEMENT
Contract Reference: C-2026-0001
Effective Date: 2026-01-01   Initial Term: 24 months
Between: Northwind Analytics Ltd ("Supplier") and Ballymore Foods plc ("Client")

CLAUSES
Termination: Either party may terminate this Agreement upon ninety (90) days' prior written notice.
Governing Law: This Agreement shall be governed by the laws of Ireland.
Liability Cap: The Supplier's aggregate liability shall not exceed EUR 1,000,000 in the aggregate.
Data Protection: The Supplier shall process client information in accordance with applicable law.

SCHEDULE 3 - PERSONAL DATA
Data Controller contact: Aoife Byrne, aoife@example.com, +353 1 555 0142
Employee IDs: EMP-441273, EMP-551802
"""


def prompt_of(body: dict) -> str:
    return body["messages"][-1]["content"]


def test_complexity_labels_match_step_types():
    # Mirrors the labels the real llm-d-sc classifier produces on these prompt
    # shapes (calibrated on the Linux stack; see tools/probe_labels.py).
    assert classify_complexity("What is the capital of France?") == "SIMPLE"
    assert classify_complexity(prompt_of(classify_body(DOC))) == "MEDIUM"
    assert classify_complexity(prompt_of(extract_body(DOC, "termination"))) == "MEDIUM"
    assert classify_complexity(prompt_of(risk_body({"termination": "x"}, False))) == "MEDIUM"
    lines = ['- {"ref": "C-1", "risk": "low", "governing_law": "x", "data_protection": true}']
    assert classify_complexity(prompt_of(report_body(lines, 3))) == "REASONING"


def test_detect_sensitivity():
    assert detect_sensitivity(DOC) is True
    clean = DOC.split("SCHEDULE 3")[0]
    assert detect_sensitivity(clean) is False


def test_parse_content_handles_model_wrapping():
    from hybrid_demo.pipeline.review import parse_content

    clean = '{"found": true, "text": "x"}'
    assert parse_content(clean) == {"found": True, "text": "x"}
    fenced = '```json\n{"found": true, "text": "x"}\n```'
    assert parse_content(fenced) == {"found": True, "text": "x"}
    prose = 'Here is the answer:\n{"found": false}\nHope that helps!'
    assert parse_content(prose) == {"found": False}
    assert parse_content("no json at all") == {"raw": "no json at all"}
    assert parse_content(None) == {}
