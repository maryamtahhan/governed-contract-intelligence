"""Phase 1 of the PPE integration: the pipeline authenticates every step.

The gateway's praxis policy filter (manifests/base/praxis/policy.yaml and
policy-sensitive.yaml) resolves X-API-Key against manifests/base/praxis/
api-keys.yaml and fails closed on unknown or missing keys. These tests pin
the two sides together: the headers the runner sends, and the digests the
gateway accepts, so neither can drift without a test failing.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from hybrid_demo.common import DEMO_API_KEYS, api_key_for_step
from hybrid_demo.pipeline.review import request_headers

REPO_ROOT = Path(__file__).resolve().parents[2]
KEYS_FILE = REPO_ROOT / "manifests" / "base" / "praxis" / "api-keys.yaml"


def test_step_headers_carry_identity_and_session():
    headers = request_headers("extract:MSA-1.1", "general", "C-2026-0001")
    # The stage, not the full step, picks the key: extraction steps present
    # the extract identity whatever clause they quote.
    assert headers["X-API-Key"] == DEMO_API_KEYS["extract"]
    assert headers["X-Session-Id"] == "C-2026-0001"
    assert headers["X-Demo-Step"] == "extract:MSA-1.1"
    assert headers["X-Demo-Doc"] == "C-2026-0001"


def test_headers_without_doc_omit_session():
    # The report step aggregates the batch and has no single document; it
    # must not invent a session id.
    headers = request_headers("report", "judgment", None)
    assert headers["X-API-Key"] == DEMO_API_KEYS["report"]
    assert "X-Session-Id" not in headers
    assert "X-Demo-Doc" not in headers


def test_unknown_stage_falls_back_to_eval_key():
    # Anything that is not a pipeline step (ad-hoc tooling reusing the
    # runner's header builder) presents the eval identity rather than
    # silently borrowing a step's.
    assert api_key_for_step("something-else") == DEMO_API_KEYS["eval"]


def test_demo_keys_match_gateway_digests():
    """The keys the runner sends must be the ones the gateway accepts.

    api-keys.yaml stores SHA-256 digests; the plaintext lives only in
    DEMO_API_KEYS. If either side changes without the other, every request
    would fail closed at the gateway — this test is where that breaks first.
    """
    directory = yaml.safe_load(KEYS_FILE.read_text())["keys"]
    accepted = {entry["user"]: entry["hash"] for entry in directory}
    expected_users = {f"gci-{name}" for name in DEMO_API_KEYS}
    assert set(accepted) == expected_users, "key directory and DEMO_API_KEYS disagree"
    for name, key in DEMO_API_KEYS.items():
        digest = "sha256:" + hashlib.sha256(key.encode()).hexdigest()
        assert accepted[f"gci-{name}"] == digest, f"digest mismatch for {name}"
