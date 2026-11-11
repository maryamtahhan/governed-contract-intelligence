"""CUAD question text and clause aliases, kept import-light.

This module has no torch/transformers import so the mapping can be unit
tested (and reused by tooling) without the specialist image installed.
"""
from __future__ import annotations

import os
import re
import sys

QUESTION_RE = re.compile(r"[Ee]xtract the ([a-z_ -]+?) clause")

# The official CUAD question wording, verbatim: the checkpoints were trained
# with these exact strings, so fidelity here measurably affects quality.
CUAD_QUESTIONS = {
    "termination": ('Highlight the parts (if any) of this contract related to "Termination For '
                    'Convenience" that should be reviewed by a lawyer. Details: Can a party terminate '
                    'this  contract without cause (solely by giving a notice and allowing a waiting  '
                    'period to expire)?'),
    "governing law": ('Highlight the parts (if any) of this contract related to "Governing Law" that '
                      'should be reviewed by a lawyer. Details: Which state/country\'s law governs the '
                      'interpretation of the contract?'),
    "liability cap": ('Highlight the parts (if any) of this contract related to "Cap On Liability" '
                      'that should be reviewed by a lawyer. Details: Does the contract include a cap '
                      'on liability upon the breach of a party’s obligation? This includes time '
                      'limitation for the counterparty to bring claims or maximum amount for recovery.'),
    "audit rights": ('Highlight the parts (if any) of this contract related to "Audit Rights" that '
                     'should be reviewed by a lawyer. Details: Does a party have the right to  audit '
                     'the books, records, or physical locations of the counterparty to ensure '
                     'compliance with the contract?'),
}

# CUAD annotates "Termination For Convenience" but not "Termination For
# Cause". The synthetic corpus contains both forms. When the canonical
# convenience question finds nothing and the contract has cause language, ask
# this focused follow-up instead of changing the canonical question (which
# preserves the checkpoint's trained wording on the 152 convenience clauses).
# The trigger is deliberately narrow: a generic "breach" match would fire on
# unrelated representations/warranties clauses.
CAUSE_TERMINATION_RE = re.compile(
    r"\b(material breach|uncured|termination for cause|for cause)\b", re.IGNORECASE)
CAUSE_TERMINATION_QUESTION = (
    'Highlight the parts (if any) of this contract related to "Termination For Cause" '
    "that should be reviewed by a lawyer. Details: Can a party terminate this contract "
    "for cause, for example because the other party committed a material breach that was "
    "not cured within the notice period?"
)

# Optional decision-model trigger. The regex is the measured default; the Jev
# candidate is opt-in because a broader trigger can add false positives on
# contracts whose only "breach" language sits in a representation or warranty.
# Adoption is gated on CUAD test-split termination F1 >= 0.714 (the regex
# baseline); run `hybrid-cause-bench` before flipping HYBRID_CAUSE_DECIDER.
CAUSE_JEV_QUESTION = (
    "Does this contract contain language that allows a party to terminate the "
    "contract for cause, such as a material breach, default, or failure to "
    "perform?"
)
CAUSE_DEFAULT_THRESHOLD = 0.5
_CAUSE_WARNED = False


def cause_jev_score(context: str) -> float:
    """Ask Jev whether the contract has termination-for-cause language.

    The endpoint can be a direct System One service (HYBRID_CAUSE_JEV_URL) or
    the Praxis screen listener. In the latter case HYBRID_CAUSE_JEV_STEP adds
    the routing header and the screen API key, and the body carries the
    stack's model name only so the gateway's model-scoped policy route
    matches; the Jev service ignores it.
    """
    from .. import pii
    from ..common import api_key_for_step

    url = (os.environ.get("HYBRID_CAUSE_JEV_URL")
           or os.environ.get("HYBRID_JEV_URL") or "").strip()
    # No URL means the local Laya runtime (if installed); the offline
    # benchmark uses this path. A production specialist image does not install
    # Laya, so it should set HYBRID_CAUSE_JEV_URL (or HYBRID_JEV_URL).
    step = os.environ.get("HYBRID_CAUSE_JEV_STEP", "decide:cause").strip()
    headers: dict[str, str] = {}
    body: dict = {}
    if step:
        headers["X-Demo-Step"] = step
        headers["X-API-Key"] = (os.environ.get("HYBRID_CAUSE_JEV_KEY")
                                or api_key_for_step("screen"))
        body["model"] = os.environ.get(
            "HYBRID_CAUSE_JEV_POLICY_MODEL", "Semantic-Qwen")
    return pii.jev_question_score(
        context,
        CAUSE_JEV_QUESTION,
        url=url,
        model=os.environ.get("HYBRID_CAUSE_JEV_MODEL") or None,
        question="cause",
        extra_headers=headers or None,
        extra_body=body or None,
        timeout=float(os.environ.get("HYBRID_CAUSE_JEV_TIMEOUT", "30")),
    )


def cause_language_present(context: str | None, decider: str | None = None) -> bool:
    """Whether to ask the termination-for-cause follow-up.

    ``regex`` (default) is the narrow, measured trigger.  ``jev`` is the
    opt-in decision-model candidate; any model/serving failure falls back to
    the regex so a broken screen cannot silently change the specialist's
    answer.  The fallback is visible on stderr once per process.
    """
    text = context or ""
    name = (decider or os.environ.get("HYBRID_CAUSE_DECIDER", "regex")).strip().lower()
    if name == "regex":
        return bool(CAUSE_TERMINATION_RE.search(text))
    if name != "jev":
        raise ValueError(f"unknown cause decider {name!r}; choose regex or jev")
    try:
        threshold = float(os.environ.get(
            "HYBRID_CAUSE_JEV_THRESHOLD", str(CAUSE_DEFAULT_THRESHOLD)))
        return cause_jev_score(text) >= threshold
    except Exception as error:  # noqa: BLE001 - fall back to the measured trigger
        global _CAUSE_WARNED
        if not _CAUSE_WARNED:
            print(f"specialist: cause decider 'jev' failed ({error}); using "
                  "the regex trigger", file=sys.stderr)
            _CAUSE_WARNED = True
        return bool(CAUSE_TERMINATION_RE.search(text))

# CUAD has no data-protection category. Do NOT alias "data protection" to
# audit rights: that silently answers a different question.
CLAUSE_ALIASES = {
    "termination": "termination",
    "governing law": "governing law",
    "governing_law": "governing law",
    "liability cap": "liability cap",
    "liability_cap": "liability cap",
    "audit rights": "audit rights",
    "audit_rights": "audit rights",
}


def clause_from_prompt(messages: list[dict]) -> str | None:
    """Parse the clause name out of the pipeline's extraction prompt."""
    for message in reversed(messages or []):
        content = message.get("content", "") if isinstance(message, dict) else ""
        m = QUESTION_RE.search(content)
        if m:
            clause = m.group(1).strip().lower()
            return CLAUSE_ALIASES.get(clause, clause)
    return None


def question_for(clause: str) -> str | None:
    """Canonical CUAD question for a clause name; None when unsupported."""
    key = (clause or "").strip().lower()
    key = CLAUSE_ALIASES.get(key, key)
    return CUAD_QUESTIONS.get(key)


def cause_question_for(clause: str | None, context: str | None) -> str | None:
    """Focused termination-for-cause follow-up, or None when not applicable.

    The canonical CUAD question is asked first. This is only a fallback for a
    contract whose text contains explicit cause language; it never replaces
    the canonical question on ordinary convenience clauses.
    """
    key = (clause or "").strip().lower()
    key = CLAUSE_ALIASES.get(key, key)
    if key != "termination" or not context:
        return None
    if not cause_language_present(context):
        return None
    return CAUSE_TERMINATION_QUESTION
