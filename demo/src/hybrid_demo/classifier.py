"""Heuristics that stand in for llm-d-sc outside the container stack.

Two roles, both clearly scoped:

- ``classify_complexity`` mirrors the label set of the llm-d-sc *complexity*
  classifier (SIMPLE / MEDIUM / COMPLEX / REASONING). The real stack gets its
  labels from llm-d-sc over gRPC; this function is only used by the local
  test stub so the demo machinery can be exercised without containers, and by
  tests. It is not in the production request path.
- ``detect_sensitivity`` is the PII screen the review pipeline runs before
  choosing the gateway entry: documents whose text matches PII patterns go to
  the sensitive listener, whose pool set contains only approved in-cluster
  backends. Sensitivity is treated as a constraint (which pools may serve),
  complexity as a preference (which pool to prefer), per the routing matrix.
"""
from __future__ import annotations

import re

from .pii import is_sensitive

SIMPLE = "SIMPLE"
MEDIUM = "MEDIUM"
COMPLEX = "COMPLEX"
REASONING = "REASONING"
LABELS = (SIMPLE, MEDIUM, COMPLEX, REASONING)

REASONING_PATTERNS = [
    r"cross-document",
    r"across (?:all|the) \d+ documents",
    r"across all documents",
    r"synthesi[sz]e",
    r"overall risk report",
    r"final report",
    r"root-cause",
    r"prove that",
    r"derive the",
    r"design a",
    r"architect a",
]

MEDIUM_PATTERNS = [
    r"assess",
    r"evaluat",
    r"weigh",
    r"judg",
    r"risk level",
    r"recommend",
    r"given the extracted",
    r"prioriti[sz]e",
    r"summar(?:y|ise|ize)",
    r"compare and aggregate",
]

def _hits(patterns: list[str], text: str) -> list[str]:
    return [p for p in patterns if re.search(p, text)]


def classify_complexity(prompt: str) -> str:
    # Mirrors the labels the real llm-d-sc complexity classifier produces on
    # this workload (calibrated on the Linux stack, see tools/probe_labels.py):
    # reasoning-verb instructions -> REASONING; prompts carrying document or
    # assessment text -> MEDIUM; short factual prompts -> SIMPLE.
    scan = (prompt[:600] + "\n" + prompt[-400:]).lower()
    if _hits(REASONING_PATTERNS, scan):
        return REASONING
    if _hits(MEDIUM_PATTERNS, scan) or "contract reference:" in scan or len(prompt) > 2500:
        return MEDIUM
    return SIMPLE


def detect_sensitivity(prompt: str) -> bool:
    """The PII screen used to choose the gateway entry.

    Delegates to ``pii.is_sensitive`` so the detector is selectable
    (HYBRID_PII_DETECTOR) without changing this call site.  Default remains
    the regex screen, so recorded routing is unchanged.
    """
    return is_sensitive(prompt)
