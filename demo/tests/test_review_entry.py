from __future__ import annotations

import asyncio

from hybrid_demo.pipeline.review import ReviewRun


def select(run: ReviewRun, sensitive: bool) -> str:
    return run.entry_for_sensitivity(sensitive)[1]


def test_entry_is_selected_by_sensitivity_only():
    run = ReviewRun("http://general", "http://sensitive", 1, governance=True)
    try:
        # The step no longer selects a pool or a listener. Both a classify and
        # a risk call for the same document take the same entry; praxis reads
        # X-Demo-Step to choose the pool.
        assert select(run, False) == "general"
        assert select(run, True) == "sensitive"
        assert run.entry_for_sensitivity(True)[0] is run.sensitive
        assert run.entry_for_sensitivity(False)[0] is run.general
    finally:
        asyncio.run(run.close())


def test_ungoverned_configs_ignore_the_sensitive_entry():
    run = ReviewRun("http://general", "http://sensitive", 1, governance=False)
    try:
        assert select(run, False) == "general"
        assert select(run, True) == "general"
    finally:
        asyncio.run(run.close())


def test_sensitive_document_without_a_sensitive_entry_stays_general():
    run = ReviewRun("http://general", None, 1, governance=True)
    try:
        assert select(run, True) == "general"
    finally:
        asyncio.run(run.close())
