"""CUAD question text and clause aliases, kept import-light.

This module has no torch/transformers import so the mapping can be unit
tested (and reused by tooling) without the specialist image installed.
"""
from __future__ import annotations

import re

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
    if not CAUSE_TERMINATION_RE.search(context):
        return None
    return CAUSE_TERMINATION_QUESTION
