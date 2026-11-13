from __future__ import annotations

import json

import pytest

from hybrid_demo import configs
from hybrid_demo.pipeline import review
from hybrid_demo.ui.payload import build_payload


@pytest.mark.parametrize("value,key", [
    ("A", "A"), ("a", "A"), ("one-pool", "A"), ("ONE-POOL", "A"),
    ("all-gpu", "A"), ("ALL-GPU", "A"),
    ("B", "B"), ("b", "B"), ("small-first", "B"), ("SMALL-FIRST", "B"),
    ("gpu-small", "B"), ("gpu-split", "B"),
    ("C", "C"), ("c", "C"), ("round-robin", "C"),
    ("D", "D"), ("d", "D"), ("hybrid", "D"), ("matrix", "D"),
    ("E", "E"), ("e", "E"), ("cap-split", "E"),
])
def test_every_name_alias_and_letter_resolves(value, key):
    assert configs.resolve_config(value) == key
    assert review.resolve_config(value) == key


def test_unknown_config_names_the_choices():
    with pytest.raises(ValueError, match="one-pool"):
        configs.resolve_config("gpu")


def test_catalog_keys_and_recorded_arm_values_are_unchanged():
    catalog = configs.catalog()
    # E (cap-split) is the measured liability-cap routing variant; its key
    # is recorded evidence the same way A-D are.
    assert [c["key"] for c in catalog] == ["A", "B", "C", "D", "E"]
    assert [c["name"] for c in catalog] == [
        "one-pool", "small-first", "round-robin", "hybrid", "cap-split"]
    # The old recorded mode strings are still the modes in configs.json.
    assert [c["mode"] for c in catalog] == [
        "all-gpu", "gpu-split", "round-robin", "matrix", "matrix"]


def test_legacy_arm_flag_still_dispatches(monkeypatch):
    seen: dict[str, str] = {}

    async def fake_run_arm(*args, **kwargs):
        seen["arm"] = args[4]
        return {"arm": args[4]}

    monkeypatch.setattr(review, "run_arm", fake_run_arm)
    review.main(["--gateway", "http://gateway", "--sensitive-gateway",
                 "http://sensitive", "--data", "data/synthetic",
                 "--out", "run", "--arm", "matrix"])
    assert seen["arm"] == "D"


def _payload(tmp_path, summary: dict) -> dict:
    data = tmp_path / "data"
    (data / "documents").mkdir(parents=True)
    (data / "ground_truth.json").write_text(json.dumps({"documents": []}))
    run = tmp_path / "run"
    run.mkdir()
    (run / "results.json").write_text(json.dumps({
        "summary": summary,
        "records": [],
    }))
    return build_payload(run, data)


def test_old_run_with_only_arm_gets_its_config_name(tmp_path):
    payload = _payload(tmp_path, {"arm": "D", "mode": "matrix", "docs": 0})
    assert payload["meta"]["arm"] == "D"
    assert payload["meta"]["config"] == "hybrid"
    assert payload["meta"]["config_meta"]["name"] == "hybrid"


def test_payload_exposes_the_canonical_catalog_once(tmp_path):
    payload = _payload(tmp_path, {"arm": "A", "mode": "all-gpu", "docs": 0})
    assert [c["name"] for c in payload["configs"]] == [
        "one-pool", "small-first", "round-robin", "hybrid", "cap-split"]


def test_historical_config_name_is_presented_canonically(tmp_path):
    payload = _payload(tmp_path, {
        "config": "all-gpu", "arm": "A", "mode": "all-gpu", "docs": 0,
    })
    # The run directory and recorded summary keep the historical label; the
    # page reads the canonical policy name, not the retired one.
    assert payload["meta"]["run"] == "run"
    assert payload["meta"]["config"] == "one-pool"
    assert payload["meta"]["config_meta"]["aliases"] == ["all-gpu"]


def test_hardware_comes_from_the_pool_map_not_the_route_name(tmp_path):
    payload = _payload(tmp_path, {
        "arm": "D", "mode": "matrix", "docs": 0,
        "routes": {"vllm-cpu": 10},
        "pools": {
            "vllm-cpu": {"model": "Qwen3.5-35B-A3B",
                         "hardware": "cpu (x8i.24xlarge)", "deployment": "cpu"},
            # Configured but never used: it must not be reported as this run's.
            "vllm-gpu": {"model": "Qwen3.5-35B-A3B",
                         "hardware": "4x A10G", "deployment": "gpu"},
        },
    })
    assert payload["meta"]["pool_hardware"] == [{
        "route": "vllm-cpu",
        "model": "Qwen3.5-35B-A3B",
        "hardware": "cpu (x8i.24xlarge)",
        "deployment": "cpu",
    }]


def test_run_without_a_pool_map_has_no_hardware_claim(tmp_path):
    payload = _payload(tmp_path, {
        "arm": "D", "mode": "matrix", "docs": 0,
        "routes": {"vllm-cpu": 10, "vllm-gpu": 1},
    })
    assert payload["meta"]["pool_hardware"] == []
