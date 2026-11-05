"""Paired significance testing.

The original tool hand-rolled the exact McNemar binomial tail.  That is
correct and dependency-free, so it stays as the fallback; when statsmodels is
installed the same computation is delegated to
``statsmodels.stats.contingency_tables.mcnemar`` and the backend is recorded
in the result.  The point is reproducibility: a paper-standard implementation
can be cited without changing the small-sample exactness.
"""
from __future__ import annotations

import math


def exact_mcnemar(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for discordant counts b and c."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2.0 * tail)


def mcnemar(b: int, c: int, backend: str = "auto") -> dict:
    """McNemar test with an explicit backend choice.

    ``backend='auto'`` uses statsmodels when available, otherwise the exact
    binomial tail.  ``backend='exact'`` always uses the fallback.
    """
    if backend not in ("auto", "statsmodels", "exact"):
        raise ValueError(f"unknown backend {backend!r}")
    used = "exact"
    p_value = exact_mcnemar(b, c)
    statistic = None
    if backend in ("auto", "statsmodels"):
        try:
            from statsmodels.stats.contingency_tables import mcnemar as sm_mcnemar

            both = 0
            neither = 0
            table = [[both, b], [c, neither]]
            result = sm_mcnemar(table, exact=True, correction=False)
            p_value = float(result.pvalue)
            statistic = float(result.statistic)
            used = "statsmodels"
        except ImportError:
            if backend == "statsmodels":
                raise
    return {"b": b, "c": c, "p_value": p_value, "statistic": statistic,
            "backend": used}


def compare_hits(a: dict[str, bool], b: dict[str, bool], k: int = 5,
                 backend: str = "auto") -> dict:
    """Compare two per-pair hit maps (as written by the retrieval evaluator)."""
    shared = sorted(set(a) & set(b))
    if not shared:
        raise ValueError("the two runs share no pairs")
    only_a = sum(1 for key in shared if a[key] and not b[key])
    only_b = sum(1 for key in shared if b[key] and not a[key])
    both = sum(1 for key in shared if a[key] and b[key])
    neither = len(shared) - only_a - only_b - both
    test = mcnemar(only_a, only_b, backend=backend)
    return {
        "k": k,
        "pairs": len(shared),
        "excluded": len(set(a) ^ set(b)),
        "hit_a": only_a + both,
        "hit_b": only_b + both,
        "both": both,
        "only_a": only_a,
        "only_b": only_b,
        "neither": neither,
        "p_value": test["p_value"],
        "statistic": test["statistic"],
        "backend": test["backend"],
    }
