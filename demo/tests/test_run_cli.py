from __future__ import annotations

from hybrid_demo.pipeline import review, run


def test_config_names_and_legacy_letters_resolve():
    assert review.resolve_config("all-gpu") == "A"
    assert review.resolve_config("gpu-small") == "B"
    assert review.resolve_config("round-robin") == "C"
    assert review.resolve_config("hybrid") == "D"
    assert review.resolve_config("D") == "D"


def test_direct_target_passes_the_remaining_options(monkeypatch):
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(run.direct, "main",
                        lambda argv: seen.setdefault("direct", argv))

    run.main(["--target", "direct", "--endpoint", "http://127.0.0.1:8000/v1",
              "--out", "run"])

    assert seen["direct"] == ["--endpoint", "http://127.0.0.1:8000/v1",
                              "--out", "run"]


def test_gateway_target_passes_the_remaining_options(monkeypatch):
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(run.review, "main",
                        lambda argv: seen.setdefault("gateway", argv))

    run.main(["--target", "gateway", "--gateway", "http://relay:8080",
              "--config", "hybrid", "--out", "run"])

    assert seen["gateway"] == ["--gateway", "http://relay:8080",
                               "--config", "hybrid", "--out", "run"]
