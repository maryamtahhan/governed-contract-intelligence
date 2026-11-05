from __future__ import annotations

import pytest

from hybrid_demo import pii


def test_regex_findings_have_offsets_and_entities():
    text = "Contact a@b.com or EMP-1234 about personal data."
    findings = pii.regex_detect(text)
    entities = {f.entity for f in findings}
    assert "EMAIL_ADDRESS" in entities
    assert "EMPLOYEE_ID" in entities
    assert "PERSONAL_DATA_MARKER" in entities
    for finding in findings:
        assert text[finding.start:finding.end]
        assert finding.detector == "regex"


def test_default_detector_and_sensitivity(monkeypatch):
    monkeypatch.delenv("HYBRID_PII_DETECTOR", raising=False)
    assert pii.resolve_detector() == "regex"
    assert pii.is_sensitive("nothing to see here") is False
    assert pii.is_sensitive("write to a@b.com") is True


def test_unknown_detector_is_rejected():
    with pytest.raises(ValueError):
        pii.detect("text", "not-a-detector")
    with pytest.raises(ValueError):
        pii.is_sensitive("text", "not-a-detector")


def test_compare_reports_confusion_when_truth_is_supplied():
    result = pii.compare(
        [("d1", "a@b.com"), ("d2", "plain contract text")],
        detectors=["regex"],
        truth={"d1": True, "d2": False},
    )
    row = result["results"]["regex"]
    assert row["documents_flagged"] == 1
    assert row["confusion"] == {
        "tp": 1, "fp": 0, "tn": 1, "fn": 0,
        "precision": 1.0, "recall": 1.0,
    }


def test_presidio_unavailable_is_explicit():
    if pii.available()["presidio"]:
        pytest.skip("presidio is installed in this environment")
    with pytest.raises(pii.DetectorUnavailable):
        pii.presidio_detect("a@b.com")
    # The routing screen fails closed rather than silently returning False.
    assert pii.is_sensitive("a@b.com", "presidio") is True


def test_taxonomy_bridge_contract(monkeypatch):
    import httpx

    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["body"] = kwargs["json"]
        return httpx.Response(
            200,
            json={"findings": [{"start": 0, "end": 4, "entity": "REGULATED"}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    findings = pii.taxonomy_detect("text", url="http://taxonomy")
    assert findings[0].entity == "REGULATED"
    assert seen["body"] == {"text": "text"}
