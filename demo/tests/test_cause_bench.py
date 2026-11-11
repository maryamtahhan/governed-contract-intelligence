from __future__ import annotations

from pathlib import Path

import pytest

from hybrid_demo.evalkit import cause_bench

DATA = Path(__file__).resolve().parents[1] / "data" / "cuad-test"


def test_score_rows_uses_the_validator_rule():
    result = cause_bench.score_rows([
        ("a", "a"),        # TP
        (None, "x"),       # FP on an absent clause
        ("b", None),       # FN
        ("c", "d"),        # wrong span -> FP, matching validate_specialist.py
    ])
    assert (result["tp"], result["fp"], result["fn"]) == (1, 2, 1)
    assert result["precision"] == 0.333
    assert result["recall"] == 0.5
    assert result["f1"] == 0.4


def test_load_cuad_docs_matches_the_recorded_subset():
    if not DATA.exists():
        pytest.skip("CUAD test subset not present")
    docs = cause_bench.load_cuad_docs(DATA)
    # The corpus has one duplicate title; the recorded run has 19 unique.
    assert len(docs) == 19
    assert sum(doc["truth"] is not None for doc in docs) == 5
    assert len({doc["doc_id"] for doc in docs}) == 19


def test_format_table_marks_the_guardrail():
    result = {
        "corpus": "CUAD test split", "documents": 19,
        "termination_present": 5,
        "measurement": "offline static batch, local CPU; specialist weights",
        "guardrail_f1": 0.714,
        "deciders": {
            "regex": {"tp": 5, "fp": 4, "fn": 0, "precision": 0.556,
                      "recall": 1.0, "f1": 0.714},
            "jev@0.5": {"tp": 5, "fp": 6, "fn": 0, "precision": 0.455,
                        "recall": 1.0, "f1": 0.625},
        },
    }
    table = cause_bench.format_table(result)
    assert "regex" in table and "jev@0.5" in table
    assert "0.714" in table and "0.625" in table
    assert table.count("yes") == 1
    assert table.count("no") == 1
