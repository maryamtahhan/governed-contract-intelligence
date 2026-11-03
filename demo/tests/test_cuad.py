"""Real-data tests for the CUAD converter.

CUAD is not committed (18MB tarball, CC BY 4.0 - verify before redistributing).
These tests run against demo/data/cuad (fetch with scripts/data/fetch-cuad.sh)
and skip cleanly when it is absent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hybrid_demo.pipeline.cuad import CLAUSE_KEYS, convert, load_cuad_json

CUAD_DIR = Path(__file__).resolve().parents[1] / "data" / "cuad"


def cuad_available() -> bool:
    try:
        load_cuad_json(CUAD_DIR)
        return True
    except SystemExit:
        return False


pytestmark = pytest.mark.skipif(not cuad_available(), reason="CUAD not downloaded (scripts/data/fetch-cuad.sh)")


def test_load_cuad_json_shape():
    data = load_cuad_json(CUAD_DIR)
    assert len(data) == 510
    para = data[0]["paragraphs"][0]
    assert "context" in para and "qas" in para
    assert len(para["context"]) > 400


def test_convert_maps_real_annotations(tmp_path):
    stats = convert(CUAD_DIR, tmp_path, count=40, seed=42, max_chars=24000)
    manifest = json.loads((tmp_path / "ground_truth.json").read_text())

    assert manifest["clause_keys"] == CLAUSE_KEYS == [
        "termination", "governing_law", "liability_cap", "audit_rights"]
    assert stats["count"] == 40
    assert stats["with_all_four_clauses"] > 0, "no document carried all four clauses"

    found = {key: 0 for key in CLAUSE_KEYS}
    for doc in manifest["documents"]:
        assert doc["type"] is None
        assert doc["source"] == "cuad"
        text = (tmp_path / "documents" / f"{doc['doc_id']}.txt").read_text()
        assert len(text) <= 24000
        for key, value in doc["clauses"].items():
            if value:
                found[key] += 1
                assert len(value) > 5
    for key, count in found.items():
        assert count > 0, f"no {key} annotations mapped at all"
    assert stats["sensitivity_annotated"] is False
    for doc in manifest["documents"]:
        # CUAD has no PII labels; do not invent a screen result.
        assert doc["sensitive"] is None


def test_convert_deterministic_and_filters_long(tmp_path):
    a = convert(CUAD_DIR, tmp_path / "a", count=30, seed=7, max_chars=24000)
    b = convert(CUAD_DIR, tmp_path / "b", count=30, seed=7, max_chars=24000)
    assert a == b
    ma = json.loads((tmp_path / "a" / "ground_truth.json").read_text())
    mb = json.loads((tmp_path / "b" / "ground_truth.json").read_text())
    assert ma["documents"] == mb["documents"]
    assert a["skipped_long"] > 0, "expected some long contracts to be filtered"
    for doc in ma["documents"]:
        assert len((tmp_path / "a" / "documents" / f"{doc['doc_id']}.txt").read_text()) <= 24000


def test_pipeline_reads_clause_keys_from_dataset(tmp_path):
    from hybrid_demo.pipeline.review import load_documents

    convert(CUAD_DIR, tmp_path, count=6, seed=42, max_chars=24000)
    docs, texts, clause_keys = load_documents(str(tmp_path))
    assert clause_keys == CLAUSE_KEYS
    assert len(docs) == 6
    assert all(len(texts[d["doc_id"]]) > 400 for d in docs)


def test_extraction_prompt_uses_dataset_clauses(tmp_path):
    from hybrid_demo.pipeline.review import extract_body, load_documents

    convert(CUAD_DIR, tmp_path, count=3, seed=42, max_chars=24000)
    docs, texts, clause_keys = load_documents(str(tmp_path))
    body = extract_body(texts[docs[0]["doc_id"]], clause_keys[3].replace("_", " "))
    assert "Extract the audit rights clause" in body["messages"][-1]["content"]
