"""Span/quote scoring, with a nervaluate adapter.

The demo's original extraction metric is *containment*: a prediction is
correct when either normalised string contains the other.  That is the right
primary metric for a copy task, but it hides boundary behaviour and it is the
only number the report has ever shown.  CUAD's annotations themselves are
noisy (FINDINGS §3.2), so a single number cannot separate "the model missed
the clause" from "the annotator chose a different boundary".

This module reports three views of the same pairs:

* ``exact``       - normalised string equality
* ``containment`` - the historical metric (either string contains the other)
* ``partial``     - token-set Jaccard >= 0.5 (a wider/narrower quote)

When document text is available it also emits nervaluate's four SemEval
strategies (strict / exact / partial / type) over character offsets.  The
adapter is optional: without nervaluate, the text-level views still work and
``offset_backend`` says ``unavailable`` rather than silently changing the
metric.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..common import normalize_text

MODES = ("exact", "containment", "partial")
_TOKEN = re.compile(r"[a-z0-9]+")
_WS = re.compile(r"\s+")


@dataclass
class ModeScore:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def as_dict(self) -> dict:
        return {"precision": round(self.precision, 3),
                "recall": round(self.recall, 3),
                "f1": round(self.f1, 3),
                "tp": self.tp, "fp": self.fp, "fn": self.fn}


def tokens(text: str | None) -> set[str]:
    return set(_TOKEN.findall(normalize_text(text)))


def token_overlap(predicted: str | None, truth: str | None) -> float:
    """Jaccard over word tokens; 1.0 for two empty strings is not useful, so
    an empty side scores 0.0."""
    p, t = tokens(predicted), tokens(truth)
    if not p or not t:
        return 0.0
    return len(p & t) / len(p | t)


def text_matches(predicted: str | None, truth: str | None,
                 mode: str = "containment") -> bool:
    if not predicted or not truth:
        return False
    p, t = normalize_text(predicted), normalize_text(truth)
    if mode == "exact":
        return p == t
    if mode == "containment":
        return p == t or p in t or t in p
    if mode == "partial":
        return token_overlap(predicted, truth) >= 0.5
    raise ValueError(f"unknown span mode {mode!r}")


def locate(text: str, needle: str, start: int = 0) -> tuple[int, int] | None:
    """Find ``needle`` in ``text`` and return (start, end).

    Tries an exact search first, then a whitespace-normalised search.  The
    latter is what makes a model answer that collapsed newlines still map to
    the source document; if neither works the caller falls back to text-level
    matching rather than inventing an offset.
    """
    if not text or not needle:
        return None
    idx = text.find(needle, start)
    if idx >= 0:
        return idx, idx + len(needle)
    # Whitespace-normalised: build a map from normalised index -> original.
    compact_chars: list[str] = []
    positions: list[int] = []
    in_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not in_space and compact_chars:
                compact_chars.append(" ")
                positions.append(i)
            in_space = True
            continue
        in_space = False
        compact_chars.append(ch.lower())
        positions.append(i)
    compact = "".join(compact_chars)
    needle_compact = _WS.sub(" ", needle.strip()).lower()
    idx = compact.find(needle_compact)
    if idx < 0:
        return None
    end = idx + len(needle_compact)
    return positions[idx], positions[min(end, len(positions) - 1)] + 1


def score_text_pairs(pairs: Iterable[tuple[str | None, str | None]],
                     mode: str = "containment") -> dict:
    score = ModeScore()
    for predicted, truth in pairs:
        if not predicted and not truth:
            continue
        if text_matches(predicted, truth, mode):
            score.tp += 1
        else:
            score.fp += 1 if predicted else 0
            score.fn += 1 if truth else 0
    return {"mode": mode, **score.as_dict()}


def nervaluate_scores(true_docs: Sequence[Sequence[dict]],
                      pred_docs: Sequence[Sequence[dict]],
                      tag: str = "CLAUSE") -> dict | None:
    """Run nervaluate over per-document entity lists.

    Each entity is ``{"start": int, "end": int, "label": tag}``.  Returns a
    dict keyed by SemEval strategy, or ``None`` when nervaluate is absent.
    """
    try:
        from nervaluate import Evaluator
    except ImportError:
        return None
    if not true_docs:
        return None
    evaluator = Evaluator(true_docs, pred_docs, tags=[tag])
    result = evaluator.evaluate().get("overall", {})
    out: dict[str, dict] = {}
    for strategy in ("strict", "exact", "partial", "ent_type"):
        value = result.get(strategy)
        if value is None:
            continue
        out[strategy] = {
            "precision": round(float(value.precision), 3),
            "recall": round(float(value.recall), 3),
            "f1": round(float(value.f1), 3),
            "correct": int(value.correct),
            "incorrect": int(value.incorrect),
            "partial": int(value.partial),
            "missed": int(value.missed),
            "spurious": int(value.spurious),
        }
    return out


def score_records(records: list[dict], truth_docs: dict[str, dict],
                  doc_texts: dict[str, str] | None = None) -> dict:
    """Score extraction records across all text modes (+ offsets when possible).

    Unserved calls are excluded for the same reason as in ``score.py``: a
    timeout is a missing field, not a wrong answer.
    """
    pairs: list[tuple[str | None, str | None]] = []
    offset_true: dict[str, list[dict]] = {}
    offset_pred: dict[str, list[dict]] = {}
    absence_correct = absence_total = 0
    for record in records:
        if not record.get("step", "").startswith("extract:"):
            continue
        if record.get("outcome", "served") != "served":
            continue
        clause = record["step"].split(":", 1)[1]
        truth = (truth_docs.get(record["doc_id"], {}).get("clauses") or {}).get(clause)
        content = record.get("content") or {}
        found = bool(content.get("found"))
        text = content.get("text") if found else None
        if truth is None:
            absence_total += 1
            absence_correct += int(not found)
            continue
        pairs.append((text, truth))
        if not doc_texts:
            continue
        source = doc_texts.get(record["doc_id"], "")
        truth_span = locate(source, truth)
        pred_span = locate(source, text) if text else None
        doc = record["doc_id"]
        if truth_span:
            offset_true.setdefault(doc, []).append(
                {"start": truth_span[0], "end": truth_span[1], "label": "CLAUSE"})
        if pred_span:
            offset_pred.setdefault(doc, []).append(
                {"start": pred_span[0], "end": pred_span[1], "label": "CLAUSE"})
    by_mode = {mode: score_text_pairs(pairs, mode) for mode in MODES}
    offset = None
    offset_backend = "unavailable"
    if doc_texts and offset_true:
        docs = sorted(set(offset_true) | set(offset_pred))
        scores = nervaluate_scores([offset_true.get(d, []) for d in docs],
                                   [offset_pred.get(d, []) for d in docs])
        if scores:
            offset = scores
            offset_backend = "nervaluate"
        else:
            offset_backend = "not_installed"
    return {
        "pairs": len(pairs),
        "by_mode": by_mode,
        "absence": {"correct": absence_correct, "total": absence_total},
        "offset": offset,
        "offset_backend": offset_backend,
    }
