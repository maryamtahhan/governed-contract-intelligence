from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = REPO_ROOT / "sandbox" / "policy.yaml"
SCRIPT = REPO_ROOT / "scripts" / "03-create-sandbox.sh"


def test_sandbox_can_reach_only_the_trace_ui_ingress():
    policies = yaml.safe_load(POLICY.read_text())["network_policies"]

    assert "praxis" not in policies
    endpoints = policies["trace_ui"]["endpoints"]
    assert {(e["host"], e["port"]) for e in endpoints} == {
        ("trace-ui.openshell-demo.svc.cluster.local", 8080),
        ("trace-ui.openshell-demo.svc.cluster.local", 8083),
    }
    assert all(e["access"] == "read-write" for e in endpoints)
    # The GitHub read-only exception is separate and deliberate.
    assert policies["github_api"]["endpoints"][0]["access"] == "read-only"


def test_sandbox_base_url_uses_trace_ui_not_praxis():
    script = SCRIPT.read_text()
    assert "trace-ui.${NS}.svc.cluster.local:8080/v1" in script
    assert "praxis.${NS}.svc.cluster.local:80/v1" not in script


def test_sandbox_uses_a_policy_key_and_sends_x_api_key():
    script = SCRIPT.read_text()
    assert "vllm-local" not in script
    assert "HYBRID_API_KEY_EVAL:-gci-demo-key-eval" in script
    assert "X-API-Key" in script
    assert "tools.profile minimal" in script
    assert "contextWindow" in script
    assert "maxTokens" in script
