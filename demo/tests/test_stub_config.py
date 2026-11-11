from __future__ import annotations

from pathlib import Path

from stub_praxis import parse_config

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPO_ROOT / "manifests" / "base" / "praxis"
MAP = {
    "trace-ui:8081": "http://127.0.0.1:1",
    "trace-ui:8082": "http://127.0.0.1:2",
    "trace-ui:8084": "http://127.0.0.1:4",
    "trace-ui:8085": "http://127.0.0.1:5",
    "trace-ui:8088": "http://127.0.0.1:8",
    "trace-ui:8089": "http://127.0.0.1:9",
}


def routes(chain: dict) -> dict[str, str]:
    return {tuple(sorted(r["headers"].items())): r["cluster"] for r in chain["routes"]}


def test_matrix_config_parses():
    parsed = parse_config(str(CONFIGS / "config.yaml"), MAP)
    assert set(parsed["listeners"]) == {8080, 8083, 8086}
    general = parsed["listeners"][8080]
    sensitive = parsed["listeners"][8083]
    screen = parsed["listeners"][8086]

    assert general["kind"] == "matrix"
    assert sensitive["kind"] == "matrix"

    expected_specialist = {
        (("x-demo-step", "extract:termination"),),
        (("x-demo-step", "extract:governing_law"),),
        (("x-demo-step", "extract:liability_cap"),),
        (("x-demo-step", "extract:audit_rights"),),
    }
    general_routes = routes(general)
    for key in expected_specialist:
        assert general_routes[key] == "cuad-specialist"
    assert general_routes[(("x-demo-step", "extract:data_protection"),)] == "vllm-cpu"
    assert general_routes[(("x-demo-step", "classify"),)] == "vllm-cpu"
    assert general_routes[(("x-demo-step", "risk"),)] == "vllm-gpu"
    assert general_routes[(("x-demo-step", "report"),)] == "vllm-gpu"
    # The unlabelled route is the catch-all, not the specialist.
    assert general_routes[()] == "vllm-cpu"
    assert general["failover"] == "vllm-gpu"

    sensitive_routes = routes(sensitive)
    for key in expected_specialist:
        assert sensitive_routes[key] == "cuad-specialist"
    assert sensitive_routes[(("x-demo-step", "extract:data_protection"),)] == "vllm-cpu"
    assert sensitive_routes[(("x-demo-step", "classify"),)] == "vllm-cpu"
    assert sensitive_routes[(("x-demo-step", "risk"),)] == "vllm-gpu-approved"
    assert sensitive_routes[(("x-demo-step", "report"),)] == "vllm-gpu-approved"
    assert sensitive_routes[()] == "vllm-cpu"
    assert sensitive["failover"] == "vllm-gpu-approved"

    assert parsed["clusters"]["cuad-specialist"]["endpoints"] == [MAP["trace-ui:8088"]]
    assert parsed["clusters"]["vllm-cpu"]["endpoints"] == [MAP["trace-ui:8081"]]
    assert parsed["clusters"]["vllm-gpu"]["endpoints"] == [MAP["trace-ui:8082"]]
    assert parsed["clusters"]["vllm-gpu-approved"]["endpoints"] == [MAP["trace-ui:8084"]]

    # The pre-entry screen is a third, control-plane listener: two explicit
    # steps and a screen-only catch-all, all pinned to the CPU decision pool
    # with no review failover.
    assert screen["kind"] == "matrix"
    assert screen["failover"] is None
    screen_routes = routes(screen)
    assert screen_routes[(("x-demo-step", "screen:pii"),)] == "jev-cpu"
    assert screen_routes[(("x-demo-step", "decide:cause"),)] == "jev-cpu"
    assert screen_routes[()] == "jev-cpu"
    assert parsed["clusters"]["jev-cpu"]["endpoints"] == [MAP["trace-ui:8089"]]


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
