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
