from __future__ import annotations

from pathlib import Path

from stub_praxis import parse_config

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPO_ROOT / "manifests" / "base" / "praxis"
MAP = {"trace-ui:8081": "http://127.0.0.1:1", "trace-ui:8082": "http://127.0.0.1:2",
       "trace-ui:8084": "http://127.0.0.1:4", "trace-ui:8085": "http://127.0.0.1:5"}


def test_matrix_config_parses():
    parsed = parse_config(str(CONFIGS / "config.yaml"), MAP)
    assert set(parsed["listeners"]) == {8080, 8083, 8086, 8087}
    general = parsed["listeners"][8080]
    sensitive = parsed["listeners"][8083]
    judgment = parsed["listeners"][8086]
    sensitive_judgment = parsed["listeners"][8087]
    # No classifier anywhere: every entry is a pin. The general and sensitive
    # entries are CPU-first (the measured runs showed everything labelling
    # MEDIUM -> CPU, FINDINGS §15.5); the judgment entries are GPU pins.
    assert general["kind"] == "pin"
    assert general["pin"] == "vllm-cpu"
    assert general["failover"] == "vllm-any"
    assert sensitive["kind"] == "pin"
    assert sensitive["pin"] == "vllm-cpu"
    # The sensitive entry may only degrade onto the approved pool: that is
    # the fail-closed governance constraint.
    assert sensitive["failover"] == "vllm-approved-only"
    assert judgment["kind"] == "pin"
    assert judgment["pin"] == "vllm-gpu"
    assert judgment["failover"] is None
    assert sensitive_judgment["kind"] == "pin"
    assert sensitive_judgment["pin"] == "vllm-gpu-approved"
    assert sensitive_judgment["failover"] is None
    assert parsed["clusters"]["vllm-any"]["endpoints"] == [MAP["trace-ui:8081"], MAP["trace-ui:8082"]]
    assert parsed["clusters"]["vllm-approved-only"]["endpoints"] == [MAP["trace-ui:8084"]]


def test_arm_configs_parse():
    a = parse_config(str(CONFIGS / "arm-a.yaml"), MAP)
    assert a["listeners"][8080]["kind"] == "pin"
    assert a["listeners"][8080]["pin"] == "vllm-gpu"
    b = parse_config(str(CONFIGS / "arm-b.yaml"), MAP)
    assert b["listeners"][8080]["kind"] == "pin"
    assert b["listeners"][8080]["pin"] == "vllm-gpu-small"
    assert b["listeners"][8080]["failover"] == "vllm-gpu-any"
    c = parse_config(str(CONFIGS / "arm-c.yaml"), MAP)
    assert c["listeners"][8080]["kind"] == "pin"
    assert c["listeners"][8080]["pin"] == "vllm-any"
