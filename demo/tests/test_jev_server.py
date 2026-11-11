from __future__ import annotations

from argparse import Namespace

from fastapi.testclient import TestClient

from hybrid_demo import pii
from hybrid_demo.jev.server import create_app


def app(fake: bool = True):
    return create_app(Namespace(model="english", concurrency=1, fake=fake))


def body(text: str, name: str = "sensitive") -> dict:
    return {
        "model": "Semantic-Qwen",
        "state": text,
        "questions": {
            name: {"type": "noul",
                   "instructions": "Does this text contain PII?"},
        },
    }


def test_health_models_and_metrics():
    client = TestClient(app())
    health = client.get("/health").json()
    assert health["pool"] == "jev-cpu"
    assert health["backend"] == "fake"
    assert client.get("/v1/models").json()["data"][0]["id"] == "english"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "jev_requests_total" in metrics.text


def test_systemone_fake_answers_with_the_regex_shape():
    client = TestClient(app())
    hit = client.post("/v1/systemone", json=body("write to a@b.com"))
    assert hit.status_code == 200
    assert hit.json()["answers"]["sensitive"]["noul"] >= 0.5
    assert float(hit.headers["x-busy-ms"]) >= 0
    miss = client.post("/v1/systemone", json=body("plain contract"))
    assert miss.json()["answers"]["sensitive"]["noul"] < 0.5


def test_systemone_rejects_unsupported_questions():
    client = TestClient(app())
    bad = body("text")
    bad["questions"]["sensitive"]["type"] = "choice"
    assert client.post("/v1/systemone", json=bad).status_code == 400
    assert client.post("/v1/systemone", json={"state": "text"}).status_code == 400
    assert client.post("/v1/systemone", json=body("")).status_code == 400


def test_systemone_uses_the_real_backend_when_not_fake(monkeypatch):
    seen = {}

    def score(text, instructions, **kwargs):
        seen["text"] = text
        seen["question"] = kwargs["question"]
        return 0.73

    monkeypatch.setattr(pii, "jev_question_score", score)
    client = TestClient(app(fake=False))
    resp = client.post("/v1/systemone", json=body("contract", name="cause"))
    assert resp.status_code == 200
    assert resp.json()["answers"]["cause"]["noul"] == 0.73
    assert seen == {"text": "contract", "question": "cause"}
