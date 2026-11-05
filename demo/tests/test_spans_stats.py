from __future__ import annotations

import math

from hybrid_demo.evalkit import spans, stats


def test_text_matching_views_are_explicit():
    assert spans.text_matches("Payment Terms", "payment terms", "exact")
    assert not spans.text_matches("Payment Terms and more", "payment terms", "exact")
    assert spans.text_matches("Payment Terms and more", "payment terms", "containment")
    assert spans.text_matches("either party may terminate", "the party may terminate",
                              "partial")


def test_locate_handles_case_and_collapsed_whitespace():
    text = "Preamble.\n\nThe Governing   Law is New York.\n"
    found = spans.locate(text, "governing law is new york")
    assert found is not None
    start, end = found
    assert "Governing" in text[start:end]
    assert text[start:end].endswith("York.")


def test_score_records_keeps_containment_and_adds_views():
    records = [{
        "doc_id": "d1", "step": "extract:termination", "outcome": "served",
        "content": {"found": True, "text": "Either party may terminate"},
    }]
    truth = {"d1": {"clauses": {"termination": "Either party may terminate"}}}
    doc_texts = {"d1": "Preamble. Either party may terminate this agreement. End."}
    result = spans.score_records(records, truth, doc_texts)
    assert result["pairs"] == 1
    assert result["by_mode"]["exact"]["f1"] == 1.0
    assert result["by_mode"]["containment"]["f1"] == 1.0
    assert result["offset_backend"] in {"nervaluate", "not_installed"}


def test_score_records_counts_absence_separately():
    records = [{
        "doc_id": "d1", "step": "extract:termination", "outcome": "served",
        "content": {"found": False, "text": None},
    }]
    truth = {"d1": {"clauses": {"termination": None}}}
    result = spans.score_records(records, truth)
    assert result["pairs"] == 0
    assert result["absence"] == {"correct": 1, "total": 1}


def test_exact_mcnemar_matches_binomial_tail():
    assert stats.exact_mcnemar(5, 0) == 0.0625
    assert math.isclose(stats.exact_mcnemar(0, 0), 1.0)
    assert stats.mcnemar(5, 0, backend="exact")["p_value"] == 0.0625


def test_compare_hits_counts_discordant_pairs():
    result = stats.compare_hits(
        {"a": True, "b": True, "c": False},
        {"a": True, "b": False, "c": False},
        k=5, backend="exact",
    )
    assert result["pairs"] == 3
    assert result["only_a"] == 1
    assert result["only_b"] == 0
    assert result["both"] == 1
    assert result["neither"] == 1
