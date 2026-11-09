"""Core QA logic for the CUAD specialist pool.

Extractive span search over long contracts, following the original CUAD/SQuAD
evaluation recipe: slide fixed-length windows over the tokenized contract,
score every (start, end) position pair per window, keep the n-best spans,
and compare the best non-null span against the model's null (no-answer)
score before answering.
"""
from __future__ import annotations

from dataclasses import dataclass

import torch

# Re-exported so existing imports (`from hybrid_demo.specialist.qa import
# CUAD_QUESTIONS`) keep working; the definitions live in a torch-free module.
from .questions import (  # noqa: F401
    CLAUSE_ALIASES,
    CUAD_QUESTIONS,
    QUESTION_RE,
    cause_question_for,
    clause_from_prompt,
    question_for,
)


@dataclass
class Span:
    text: str
    score: float
    window: int
    null_score: float | None = None


@torch.no_grad()
def best_spans(model, tokenizer, question: str, context: str,
               max_length: int = 512, doc_stride: int = 128,
               n_best_size: int = 20, max_answer_tokens: int = 256,
               null_margin: float = 0.0, max_windows: int = 80) -> Span | None:
    """null_margin > 0 demands the answer beat the no-answer score by that
    many logits, trading recall for precision on absent clauses."""
    """Best answer span for (question, context), or None if the model leans
    no-answer. Windows are capped so pathological documents stay bounded."""
    enc = tokenizer(question, context, return_offsets_mapping=True,
                    truncation="only_second", max_length=max_length,
                    stride=doc_stride, return_overflowing_tokens=True,
                    return_tensors="pt", padding="longest")
    if "input_ids" not in enc or enc["input_ids"].shape[0] == 0:
        return None

    input_ids = enc["input_ids"][:max_windows]
    offsets = enc["offset_mapping"][:max_windows]
    keep = {k: v[:max_windows] for k, v in enc.items()
            if k not in ("offset_mapping",) and torch.is_tensor(v)}
    out = model(**keep)
    start_logits = out.start_logits
    end_logits = out.end_logits

    best: Span | None = None
    best_null = None
    pad_id = tokenizer.pad_token_id
    cls_id = tokenizer.cls_token_id

    for w in range(input_ids.shape[0]):
        ids = input_ids[w]
        offs = offsets[w].tolist()
        s_logits = start_logits[w]
        e_logits = end_logits[w]

        cls_index = (ids == cls_id).nonzero()
        cls_index = int(cls_index[0]) if cls_index.numel() else 0
        null_score = float(s_logits[cls_index] + e_logits[cls_index])
        if best_null is None or null_score > best_null:
            best_null = null_score

        n = min(n_best_size, ids.shape[0])
        s_idx = torch.topk(s_logits, n).indices.tolist()
        e_idx = torch.topk(e_logits, n).indices.tolist()

        for si in s_idx:
            if ids[si] in (pad_id, cls_id, tokenizer.sep_token_id):
                continue
            char_start = offs[si][0]
            if char_start == 0 and offs[si][1] == 0:
                continue
            for ei in e_idx:
                if ei < si or ei - si + 1 > max_answer_tokens:
                    continue
                if ids[ei] in (pad_id, cls_id, tokenizer.sep_token_id):
                    continue
                char_end = offs[ei][1]
                if char_end == 0:
                    continue
                score = float(s_logits[si] + e_logits[ei])
                if best is None or score > best.score:
                    best = Span(text=context[char_start:char_end].strip(),
                                score=score, window=w)

    if best is None:
        return None
    best.null_score = best_null
    if best_null is not None and best.score < best_null + null_margin:
        return None
    return best
