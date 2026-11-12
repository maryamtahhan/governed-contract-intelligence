"""The PPE identity and step-binding contract at the gateway.

The gateway's praxis policy filter (manifests/base/praxis/policy.yaml,
policy-sensitive.yaml and policy-screen.yaml) resolves X-API-Key against
manifests/base/praxis/api-keys.yaml and fails closed on unknown or missing
keys. The step-key plugin binds the authenticated subject to the stage in
X-Demo-Step. These tests pin the runner's headers, the key digests, and the
step grants together, so none can drift without a test failing.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from hybrid_demo.common import DEMO_API_KEYS, DEMO_KEY_STEPS, api_key_for_step
from hybrid_demo.pipeline.review import request_headers

REPO_ROOT = Path(__file__).resolve().parents[2]
PRAXIS = REPO_ROOT / "manifests" / "base" / "praxis"
KEYS_FILE = PRAXIS / "api-keys.yaml"
POLICY_FILE = PRAXIS / "policy.yaml"
SENSITIVE_POLICY_FILE = PRAXIS / "policy-sensitive.yaml"
SCREEN_POLICY_FILE = PRAXIS / "policy-screen.yaml"
PRAXIS_CONFIG_FILE = PRAXIS / "config.yaml"
KUSTOMIZATION_FILE = REPO_ROOT / "manifests" / "base" / "kustomization.yaml"


def _plugins(path: Path) -> dict[str, dict]:
    document = yaml.safe_load(path.read_text())
    return {plugin["name"]: plugin for plugin in document["plugins"]}


def _runs(path: Path) -> list[str]:
    document = yaml.safe_load(path.read_text())
    return document["routes"][0]["authorization"]["pre_invocation"]


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


def test_screen_and_decide_steps_use_the_screen_key():
    assert api_key_for_step("screen:pii") == DEMO_API_KEYS["screen"]
    assert api_key_for_step("decide:cause") == DEMO_API_KEYS["screen"]


def test_key_directory_binds_each_key_to_its_steps():
    """A pipeline key can only claim the stages it is granted.

    The gateway turns each key's groups into roles and step-key requires
    `step:<stage>`. This test makes the grant table exhaustive: a new key
    with no step grants, or a stage added to one side only, fails here.
    """
    directory = yaml.safe_load(KEYS_FILE.read_text())["keys"]
    by_user = {entry["user"]: set(entry["groups"]) for entry in directory}
    assert {user.removeprefix("gci-") for user in by_user} == set(DEMO_KEY_STEPS)
    for name, stages in DEMO_KEY_STEPS.items():
        grants = {group for group in by_user[f"gci-{name}"] if group.startswith("step:")}
        assert grants == {f"step:{stage}" for stage in stages}, name
    # The eval key is deliberately step-less. It may make a classifier call
    # without X-Demo-Step, but it cannot name a review stage.
    assert not {g for g in by_user["gci-eval"] if g.startswith("step:")}


def test_general_policy_runs_the_step_binding_and_pii_gate():
    plugins = _plugins(POLICY_FILE)
    assert plugins["step-key"]["kind"] == "validator/gci-step-key"
    assert plugins["step-key"]["mode"] == "sequential"
    assert plugins["step-key"]["config"]["stepless_roles"] == ["eval"]
    # read_subject only projects id/type; the step gate compares roles, so
    # the capability must be present or every keyed request denies.
    assert "read_roles" in plugins["step-key"]["capabilities"]
    assert plugins["pii-gate"]["kind"] == "validator/gci-pii-gate"
    assert plugins["pii-gate"]["mode"] == "sequential"
    runs = _runs(POLICY_FILE)
    assert runs == [
        "require(authenticated)",
        "run(step-key)",
        "run(pii-gate)",
        "run(audit)",
    ]


def test_sensitive_and_screen_entries_keep_observe_only_pii():
    for path in (SENSITIVE_POLICY_FILE, SCREEN_POLICY_FILE):
        plugins = _plugins(path)
        assert "pii-gate" not in plugins, path
        assert plugins["pii-observe"]["kind"] == "validator/gci-pii-observe"
        assert plugins["pii-observe"]["mode"] == "audit"
        # Only the general entry admits the step-less eval caller; a review
        # stage must be named on both the sensitive and screen entries.
        assert plugins["step-key"]["config"]["stepless_roles"] == []
        assert "read_roles" in plugins["step-key"]["capabilities"]
        assert "run(step-key)" in _runs(path)
        assert "run(pii-observe)" in _runs(path)
    # The sensitive entry still taints the session; the screen entry has no
    # sensitive label to set.
    assert "taint(sensitive, session)" in _runs(SENSITIVE_POLICY_FILE)
    assert "taint(sensitive, session)" not in _runs(SCREEN_POLICY_FILE)


def test_screen_listener_points_at_the_observe_only_policy():
    config = yaml.safe_load(PRAXIS_CONFIG_FILE.read_text())
    chains = {chain["name"]: chain for chain in config["filter_chains"]}
    policy = next(
        entry for entry in chains["screen-routing"]["filters"]
        if entry["filter"] == "policy"
    )
    assert policy["config_path"] == "/etc/praxis/policy-screen.yaml"
    general = next(
        entry for entry in chains["inference-routing"]["filters"]
        if entry["filter"] == "policy"
    )
    assert general["config_path"] == "/etc/praxis/policy.yaml"
    kustomization = yaml.safe_load(KUSTOMIZATION_FILE.read_text())
    files = kustomization["configMapGenerator"][0]["files"]
    assert "policy-screen.yaml=praxis/policy-screen.yaml" in files
