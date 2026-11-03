from __future__ import annotations

from pathlib import Path

from stub_praxis import parse_config

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPO_ROOT / "manifests" / "praxis"
MAP = {"trace-ui:8081": "http://127.0.0.1:1", "trace-ui:8082": "http://127.0.0.1:2",
       "trace-ui:8084": "http://127.0.0.1:4", "trace-ui:8085": "http://127.0.0.1:5"}


def test_matrix_config_parses():
    parsed = parse_config(str(CONFIGS / "config.yaml"), MAP)
    assert set(parsed["listeners"]) == {8080, 8083}
    general = parsed["listeners"][8080]
    sensitive = parsed["listeners"][8083]
    assert general["kind"] == "iterative"
    assert general["routes"] == {"SIMPLE": "vllm-cpu", "MEDIUM": "vllm-cpu",
                                 "COMPLEX": "vllm-gpu", "REASONING": "vllm-gpu"}
    assert general["failover"] == "vllm-any"
    assert sensitive["routes"]["REASONING"] == "vllm-gpu-approved"
    assert sensitive["routes"]["SIMPLE"] == "vllm-cpu"
    assert sensitive["failover"] == "vllm-approved-only"
    assert parsed["clusters"]["vllm-any"]["endpoints"] == [MAP["trace-ui:8081"], MAP["trace-ui:8082"]]
    assert parsed["clusters"]["vllm-approved-only"]["endpoints"] == [MAP["trace-ui:8084"]]


def test_arm_configs_parse():
    a = parse_config(str(CONFIGS / "arm-a.yaml"), MAP)
    assert a["listeners"][8080]["kind"] == "pin"
    assert a["listeners"][8080]["pin"] == "vllm-gpu"
    assert 8083 not in a["listeners"]

    b = parse_config(str(CONFIGS / "arm-b.yaml"), MAP)
    routes = b["listeners"][8080]["routes"]
    assert routes["SIMPLE"] == "vllm-gpu-small"
    assert routes["REASONING"] == "vllm-gpu"

    c = parse_config(str(CONFIGS / "arm-c.yaml"), MAP)
    chain = c["listeners"][8080]
    assert chain["pin"] == "vllm-any"
    assert len(c["clusters"]["vllm-any"]["endpoints"]) == 2
