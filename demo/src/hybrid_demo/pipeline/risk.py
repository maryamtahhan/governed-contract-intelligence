"""The single risk rubric used by the generator, the CUAD converter and the
LLM prompt.

Keeping one function means the model is asked the rule it is graded on. A
dataset may or may not annotate a data-protection clause; when it does, the
absence of that clause is high risk (the demo's product rule). CUAD has no
such category, so it falls through to the liability/termination rules.

Rule:
- high: the annotated clause set includes ``data_protection`` and it is
  absent, or liability is uncapped/absent;
- medium: termination is absent, or is only permitted for material breach;
- low: otherwise.
"""
from __future__ import annotations

DATA_PROTECTION_KEY = "data_protection"

RUBRIC_WITH_DATA_PROTECTION = (
    "Apply exactly this rubric: risk is high if there is no data-protection "
    "clause, or if liability is uncapped or absent; medium if termination is "
    "absent or only permitted for material breach that remains uncured; "
    "otherwise low."
)
RUBRIC_WITHOUT_DATA_PROTECTION = (
    "Apply exactly this rubric: risk is high if liability is uncapped or "
    "absent; medium if termination is absent or only permitted for material "
    "breach that remains uncured; otherwise low."
)


def assess_risk(clauses: dict, *, has_data_protection: bool,
                uncapped: bool = False) -> str:
    """Return low|medium|high for a clause dict under the shared rubric."""
    if has_data_protection and not clauses.get(DATA_PROTECTION_KEY):
        return "high"
    liability = (clauses.get("liability_cap") or "").lower()
    if uncapped or not liability or "unlimited" in liability:
        return "high"
    termination = (clauses.get("termination") or "").lower()
    if not termination or "material breach" in termination:
        return "medium"
    return "low"


def rubric_text(has_data_protection: bool) -> str:
    return (RUBRIC_WITH_DATA_PROTECTION if has_data_protection
            else RUBRIC_WITHOUT_DATA_PROTECTION)
