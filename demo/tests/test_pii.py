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


def test_jev_http_contract_and_threshold(monkeypatch):
    import httpx

    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["body"] = kwargs["json"]
        return httpx.Response(
            200,
            json={"answers": {"sensitive": {"type": "noul", "noul": 0.82}}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    findings = pii.jev_detect("sensitive text", url="http://jev", threshold=0.5)
    assert len(findings) == 1
    assert findings[0].entity == "PII"
    assert findings[0].detector == "jev"
    assert findings[0].score == 0.82
    assert findings[0].start == 0
    assert findings[0].end == len("sensitive text")
    assert seen["body"]["state"] == "sensitive text"
    assert seen["body"]["questions"]["sensitive"]["type"] == "noul"

    # A probability under the cutoff is not a finding, but the same score
    # passes a lower cutoff.  Thresholding is the only difference.
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: httpx.Response(
        200, json={"answers": {"sensitive": {"noul": 0.4}}},
        request=httpx.Request("POST", url)))
    assert pii.jev_detect("text", url="http://jev", threshold=0.5) == []
    assert pii.jev_detect("text", url="http://jev", threshold=0.3)


def test_jev_chunks_long_text_and_takes_the_maximum(monkeypatch):
    chunks = []

    def fake_local(chunk, model, *args, **kwargs):
        chunks.append(chunk)
        return 0.1 if len(chunks) == 1 else 0.9

    monkeypatch.setattr(pii, "_jev_local", fake_local)
    long_text = ("A" * 1200 + ". ") * 4
    assert pii.jev_score(long_text) == 0.9
    assert len(chunks) > 1
    assert all(len(chunk) <= pii.JEV_CHUNK_CHARS for chunk in chunks)


def test_screen_detector_uses_the_praxis_screen_route(monkeypatch):
    import httpx

    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        seen["body"] = kwargs["json"]
        return httpx.Response(
            200,
            json={"answers": {"sensitive": {"type": "noul", "noul": 0.91}}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("HYBRID_PII_SCREEN_URL", "http://screen/v1/systemone")
    monkeypatch.setenv("HYBRID_PII_SCREEN_KEY", "screen-key")
    monkeypatch.delenv("HYBRID_JEV_API_KEY", raising=False)
    findings = pii.detect("a@b.com", "screen")
    assert findings[0].detector == "screen"
    assert findings[0].score == 0.91
    assert seen["url"] == "http://screen/v1/systemone"
    assert seen["headers"]["X-Demo-Step"] == "screen:pii"
    assert seen["headers"]["X-API-Key"] == "screen-key"
    assert seen["body"]["model"] == "Semantic-Qwen"
    assert seen["body"]["questions"]["sensitive"]["type"] == "noul"
    assert pii.available()["screen"] is True


def test_screen_without_a_listener_fails_closed(monkeypatch):
    monkeypatch.delenv("HYBRID_PII_SCREEN_URL", raising=False)
    assert pii.available()["screen"] is False
    assert pii.is_sensitive("a@b.com", "screen") is True


def test_jev_failure_fails_closed(monkeypatch):
    def broken(chunk, model, *args, **kwargs):
        raise RuntimeError("model failed")

    monkeypatch.setattr(pii, "_jev_local", broken)
    assert pii.is_sensitive("a@b.com", "jev") is True
