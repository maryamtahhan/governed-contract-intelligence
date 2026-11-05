from __future__ import annotations

import asyncio

from hybrid_demo.pipeline.review import JUDGMENT_STEPS, ReviewRun


def select(run: ReviewRun, sensitive: bool, step: str) -> str:
    return run.entry_for_step(sensitive, step)[1]


def test_judgment_steps_are_the_judgment_stages():
    assert JUDGMENT_STEPS == {"risk", "report"}


def test_hybrid_full_split_selects_by_step_and_sensitivity():
    run = ReviewRun(
        "http://general", "http://sensitive", 1, governance=True,
        judgment_base="http://judgment",
        sensitive_judgment_base="http://sensitive-judgment",
    )
    try:
        assert select(run, False, "classify") == "general"
        assert select(run, False, "extract:termination") == "general"
        assert select(run, False, "risk") == "judgment"
        assert select(run, False, "report") == "judgment"

        assert select(run, True, "classify") == "sensitive"
        assert select(run, True, "extract:termination") == "sensitive"
        assert select(run, True, "risk") == "sensitive-judgment"
        assert select(run, True, "report") == "sensitive-judgment"

        assert run.entry_for_step(True, "risk")[0] is run.sensitive_judgment
        assert run.entry_for_step(False, "risk")[0] is run.judgment
    finally:
        asyncio.run(run.close())


def test_missing_judgment_entries_fall_back_to_stage_agnostic_entries():
    run = ReviewRun("http://general", "http://sensitive", 1, governance=True)
    try:
        assert select(run, False, "risk") == "general"
        assert select(run, False, "report") == "general"
        assert select(run, True, "risk") == "sensitive"
        assert select(run, True, "report") == "sensitive"
    finally:
        asyncio.run(run.close())


def test_sensitive_judgment_without_its_entry_uses_the_sensitive_entry():
    run = ReviewRun(
        "http://general", "http://sensitive", 1, governance=True,
        judgment_base="http://judgment",
    )
    try:
        assert select(run, True, "risk") == "sensitive"
        assert select(run, False, "risk") == "judgment"
    finally:
        asyncio.run(run.close())


def test_ungoverned_configs_ignore_judgment_entries():
    # Arms A/B/C are baselines: the judgment split belongs to the hybrid
    # policy, so passing the flags must not change their routing.
    run = ReviewRun(
        "http://general", None, 1, governance=False,
        judgment_base="http://judgment",
        sensitive_judgment_base="http://sensitive-judgment",
    )
    try:
        assert select(run, False, "risk") == "general"
        assert select(run, False, "report") == "general"
        assert select(run, True, "risk") == "general"
    finally:
        asyncio.run(run.close())
