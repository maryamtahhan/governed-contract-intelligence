from __future__ import annotations

import json

from hybrid_demo import pii
from hybrid_demo.evalkit import jev_bench


def _corpus(tmp_path):
    documents = [
        {"doc_id": "s1", "sensitive": True},
        {"doc_id": "s2", "sensitive": True},
        {"doc_id": "n1", "sensitive": False},
        {"doc_id": "n2", "sensitive": False},
    ]
    (tmp_path / "ground_truth.json").write_text(json.dumps({"documents": documents}))
    docs = tmp_path / "documents"
    docs.mkdir()
    for doc in documents:
        (docs / f"{doc['doc_id']}.txt").write_text(doc["doc_id"])
    return tmp_path


def test_benchmark_scores_regex_and_jev(tmp_path, monkeypatch):
    data = _corpus(tmp_path)
    scores = {"s1": 0.9, "s2": 0.4, "n1": 0.1, "n2": 0.6}
    monkeypatch.setattr(pii, "jev_score", lambda text, **kwargs: scores[text])
    monkeypatch.setattr(pii, "detect", lambda text, name: text in {"s1", "s2", "n2"})

    result = jev_bench.benchmark(data, thresholds=(0.5,))
    assert result["documents"] == 4
    assert result["sensitive"] == 2

    regex = result["detectors"]["regex"]["confusion"]
    assert (regex["tp"], regex["fp"], regex["tn"], regex["fn"]) == (2, 1, 1, 0)
    assert regex["precision"] == 0.667
    assert regex["recall"] == 1.0

    jev = result["detectors"]["jev"]["thresholds"]["0.5"]
    assert (jev["tp"], jev["fp"], jev["tn"], jev["fn"]) == (1, 1, 1, 1)
    assert jev["precision"] == 0.5
    assert jev["recall"] == 0.5

    table = jev_bench.format_table(result)
    assert "regex" in table and "jev" in table and "0.5" in table
