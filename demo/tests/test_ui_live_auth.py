"""The live/corpus POST routes are a proxy to --endpoint; gate and bound them.

Live mode sends request bodies the browser cannot send itself, which makes
those three routes the only ones worth refusing to serve to a network. These
tests cover the token gate, the non-loopback bind policy, the archive size
cap as the route applies it, and the live document store's bounds.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from hybrid_demo.ui.corpus import MAX_ARCHIVE_BYTES
from hybrid_demo.ui.server import LiveDocs, _live_bind_error, create_app

DATA = Path(__file__).parent / "data-e2e"
DOC_ID = "C-2026-0001"
ROUTES = ("/api/live", "/api/live/stream", "/api/corpus/stream")


def _run_dir(tmp_path: Path) -> Path:
    """One scored document: the least create_app can replay."""
    doc = next(d for d in json.loads((DATA / "ground_truth.json").read_text())["documents"]
               if d["doc_id"] == DOC_ID)
    records = [{"step": "classify", "doc_id": DOC_ID, "outcome": "served",
                "latency_ms": 4.0, "content": {"type": doc["type"]}}]
    for clause, text in doc["clauses"].items():
        records.append({"step": f"extract:{clause}", "doc_id": DOC_ID,
                        "outcome": "served", "latency_ms": 4.0,
                        "content": {"found": text is not None, "text": text or ""}})
    records.append({"step": "risk", "doc_id": DOC_ID, "outcome": "served",
                    "latency_ms": 4.0, "content": {"risk": doc["risk"], "reasons": []}})
    run = tmp_path / "run"
    run.mkdir()
    (run / "results.json").write_text(json.dumps(
        {"summary": {"arm": "direct", "mode": "direct", "docs": 1},
         "records": records}))
    return run


def _client(tmp_path: Path, **kwargs) -> TestClient:
    app = create_app(_run_dir(tmp_path), DATA, kwargs.get("endpoint"),
                     live_token=kwargs.get("live_token"))
    return TestClient(app)


# --- token gate ------------------------------------------------------------

def test_every_live_route_refuses_a_missing_or_wrong_token(tmp_path):
    client = _client(tmp_path, endpoint="http://127.0.0.1:1/v1", live_token="sesame")
    for path in ROUTES:
        assert client.post(path, json={"doc_id": DOC_ID}).status_code == 401, path
        wrong = client.post(path, json={"doc_id": DOC_ID},
                            headers={"X-Live-Token": "open-says-me"}).status_code
        assert wrong == 401, path


def test_the_token_is_accepted_as_a_header_or_a_bearer(tmp_path):
    client = _client(tmp_path, live_token="sesame")
    # No --endpoint here, so a request past the gate fails for that reason —
    # which is what proves the token was accepted rather than ignored.
    for headers in ({"X-Live-Token": "sesame"}, {"Authorization": "Bearer sesame"}):
        r = client.post("/api/live", json={"doc_id": DOC_ID}, headers=headers)
        assert r.status_code == 400, headers
        assert "without --endpoint" in r.json()["detail"]


def test_without_a_token_the_routes_behave_as_before(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/live", json={"doc_id": DOC_ID}).status_code == 400


# --- non-loopback bind -----------------------------------------------------

def test_a_non_loopback_live_bind_needs_a_token_or_an_opt_in():
    refused = _live_bind_error("0.0.0.0", "http://p:8000/v1", None, False)
    assert refused and "--live-token" in refused
    assert _live_bind_error("0.0.0.0", "http://p:8000/v1", "sesame", False) is None
    assert _live_bind_error("0.0.0.0", "http://p:8000/v1", None, True) is None
    assert _live_bind_error("10.0.0.5", "http://p:8000/v1", None, False) is not None


def test_a_loopback_or_replay_only_bind_is_always_allowed():
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.2"):
        assert _live_bind_error(host, "http://p:8000/v1", None, False) is None, host
    # No live endpoint: no proxy, so a wider bind is a replay-only server.
    assert _live_bind_error("0.0.0.0", None, None, False) is None


# --- archive cap, as the route applies it ----------------------------------

def test_an_oversize_archive_is_refused_before_any_endpoint_call(tmp_path):
    client = _client(tmp_path, endpoint="http://127.0.0.1:1/v1")
    huge = "A" * (((MAX_ARCHIVE_BYTES // 3) + 1) * 4 + 1)
    r = client.post("/api/corpus/stream",
                    json={"archive": {"name": "big.zip", "data": huge}})
    assert r.status_code == 413
    assert "base64 characters" in r.json()["detail"]


def test_an_invalid_base64_archive_is_a_400_not_a_413(tmp_path):
    client = _client(tmp_path, endpoint="http://127.0.0.1:1/v1")
    r = client.post("/api/corpus/stream",
                    json={"archive": {"name": "bad.zip", "data": "not base64!!!"}})
    assert r.status_code == 400
    assert "not valid base64" in r.json()["detail"]


# --- live document store ---------------------------------------------------

def test_live_docs_evicts_the_oldest_over_the_document_count():
    store = LiveDocs(max_docs=3)
    ids = [store.add("a.txt", "one"), store.add("b.txt", "two"),
           store.add("c.txt", "three"), store.add("d.txt", "four")]
    assert len(store) == 3
    # One document over the cap evicts exactly the oldest.
    assert ids[0] not in store
    assert ids[1] in store and ids[2] in store and ids[3] in store
    store.add("e.txt", "five")
    assert len(store) == 3
    assert ids[1] not in store and ids[2] in store


def test_live_docs_bounds_the_total_characters():
    store = LiveDocs(max_chars=10)
    first = store.add("a.txt", "x" * 6)
    second = store.add("b.txt", "x" * 6)  # 12 chars over a 10-char cap
    assert len(store) == 1
    assert first not in store and second in store


def test_live_doc_ids_stay_unique_after_eviction():
    store = LiveDocs(max_docs=2)
    ids = [store.add("same.txt", f"text {i}") for i in range(5)]
    assert len(set(ids)) == 5
