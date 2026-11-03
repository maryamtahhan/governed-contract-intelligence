from __future__ import annotations

import json
import random

from hybrid_demo.pipeline.generate import CLAUSE_KEYS, build_document, generate, risk_of


def test_generate_deterministic(tmp_path):
    a = generate(tmp_path / "a", 30, seed=42, golden=5)
    b = generate(tmp_path / "b", 30, seed=42, golden=5)
    assert a == b
    ta = json.loads((tmp_path / "a" / "ground_truth.json").read_text())
    tb = json.loads((tmp_path / "b" / "ground_truth.json").read_text())
    assert ta["documents"] == tb["documents"]


def test_generate_spread(tmp_path):
    stats = generate(tmp_path, 30, seed=42, golden=6)
    assert 0 < stats["sensitive"] < 30
    assert 0 < stats["missing_dp"] < 30
    manifest = json.loads((tmp_path / "ground_truth.json").read_text())
    docs = manifest["documents"]
    assert docs[3]["sensitive"] is True
    assert docs[5]["clauses"]["data_protection"] is None
    assert len(manifest["golden"]) == 6
    for doc in docs:
        text = (tmp_path / "documents" / f"{doc['doc_id']}.txt").read_text()
        assert f"Contract Reference: {doc['doc_id']}" in text
        for key in CLAUSE_KEYS:
            if doc["clauses"][key]:
                assert key.replace("_", " ").title() + ":" in text


def test_risk_rules():
    base = {k: "x" for k in CLAUSE_KEYS}
    assert risk_of({**base, "data_protection": None}) == "high"
    assert risk_of({**base, "liability_cap": "liability shall be unlimited"}) == "high"
    assert risk_of({**base, "termination": "terminated only for material breach uncured"}) == "medium"
    assert risk_of(base) == "low"


def test_sensitive_documents_carry_pii(tmp_path):
    generate(tmp_path, 30, seed=42, golden=6)
    manifest = json.loads((tmp_path / "ground_truth.json").read_text())
    for doc in manifest["documents"]:
        text = (tmp_path / "documents" / f"{doc['doc_id']}.txt").read_text()
        assert ("SCHEDULE 3 - PERSONAL DATA" in text) == doc["sensitive"]


def test_build_document_shape():
    text, truth = build_document(random.Random(1), "C-TEST-0001", index=0)
    assert text.startswith(("MASTER SERVICES AGREEMENT", "NON-DISCLOSURE AGREEMENT",
                            "STATEMENT OF WORK", "DATA PROCESSING AGREEMENT"))
    assert truth["doc_id"] == "C-TEST-0001"
    assert set(truth["clauses"]) == set(CLAUSE_KEYS)
    assert "Contract Reference: C-TEST-0001" in text
    assert truth["risk"] in {"low", "medium", "high"}
    for clause in truth["clauses"].values():
        assert clause is None or clause in text
