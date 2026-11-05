"""Groundedness checks for the synthesis steps.

Quality is scored against CUAD ground truth, but the report/risk answers are
also claims *about retrieved evidence*.  A model can match the rubric and
still assert something no clause supports.  This module adds a deterministic,
CPU-only groundedness check (no judge model, no API key) and an optional
RAGAS adapter for the standard faithfulness/context-precision metrics.

The lexical metric is deliberately simple and conservative:

* ``groundedness`` - fraction of answer word tokens that appear in the
  supplied evidence; quoted span text should be near 1.0, a fabricated reason
  lowers it.
* ``context_precision`` - fraction of evidence sentences that share at least
  one non-stopword with the answer; low values mean the pipeline retrieved
  noise.
* ``context_recall`` - fraction of reference tokens covered by the evidence
  (only when a reference is supplied).

It is a screen, not a substitute for RAGAS.  When RAGAS is installed,
``ragas_scores`` delegates to it and records the judge model in the result.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

_TOKEN = re.compile(r"[a-z0-9]+")
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with",
    "by", "as", "at", "is", "are", "was", "were", "be", "been", "this",
    "that", "these", "those", "it", "its", "from", "shall", "may", "will",
}


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _content_tokens(text: str) -> set[str]:
    return {token for token in _tokens(text) if token not in _STOP}


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.findall(text or "") if part.strip()]


def groundedness(answer: str, contexts: Sequence[str]) -> float:
    answer_tokens = [token for token in _tokens(answer) if token not in _STOP]
    if not answer_tokens:
        return 0.0
    evidence = _content_tokens(" ".join(contexts))
    return round(sum(token in evidence for token in answer_tokens) / len(answer_tokens), 3)


def context_precision(answer: str, contexts: Sequence[str]) -> float:
    parts = [part for context in contexts for part in sentences(context)]
    if not parts:
        return 0.0
    answer_tokens = _content_tokens(answer)
    hits = sum(bool(_content_tokens(part) & answer_tokens) for part in parts)
    return round(hits / len(parts), 3)


def context_recall(reference: str, contexts: Sequence[str]) -> float:
    reference_tokens = _content_tokens(reference)
    if not reference_tokens:
        return 0.0
    evidence = _content_tokens(" ".join(contexts))
    return round(len(reference_tokens & evidence) / len(reference_tokens), 3)


def evaluate(answer: str, contexts: Sequence[str], reference: str | None = None) -> dict:
    result = {"groundedness": groundedness(answer, contexts),
              "context_precision": context_precision(answer, contexts),
              "contexts": len(contexts), "answer_tokens": len(_tokens(answer))}
    if reference is not None:
        result["context_recall"] = context_recall(reference, contexts)
    return result


def evaluate_records(records: list[dict], data_dir: str | None = None) -> dict:
    """Groundedness for risk/report steps, using the doc's extracted clauses.

    Extraction records are the evidence the synthesis step actually received,
    so they are the right context for this check (not the whole document).
    """
    by_doc: dict[str, list[str]] = {}
    for record in records:
        if not record.get("step", "").startswith("extract:"):
            continue
        content = record.get("content") or {}
        if content.get("found") and content.get("text"):
            by_doc.setdefault(record["doc_id"], []).append(str(content["text"]))
    results = []
    for record in records:
        if record.get("step") not in ("risk", "report"):
            continue
        content = record.get("content") or {}
        if record.get("step") == "risk":
            contexts = by_doc.get(record["doc_id"], [])
        else:
            contexts = [text for texts in by_doc.values() for text in texts]
        answer = " ".join(str(value) for value in content.values()) if content else ""
        if not answer:
            continue
        results.append({"step": record["step"], "doc_id": record.get("doc_id"),
                        **evaluate(answer, contexts)})
    if not results:
        return {"samples": 0}
    keys = ("groundedness", "context_precision", "context_recall")
    summary = {key: round(sum(r.get(key, 0.0) for r in results) / len(results), 3)
               for key in keys if any(key in r for r in results)}
    return {"samples": len(results), **summary, "per_sample": results}


def ragas_available() -> bool:
    try:
        import ragas  # noqa: F401
        return True
    except ImportError:
        return False


def ragas_scores(samples: Iterable[dict[str, Any]], llm: Any = None,
                 metrics: Sequence[str] = ("faithfulness", "answer_relevancy",
                                           "context_precision"),
                 backend: str = "auto") -> dict:
    """Adapter for RAGAS; ``samples`` need user_input/response/retrieved_contexts.

    RAGAS needs a judge model.  Pass a LangChain-compatible ``llm`` (the demo
    can point one at its own OpenAI endpoint), or omit it and use RAGAS's
    default configuration.  The judge model is recorded, because a
    judge-dependent number is not comparable across judges.
    """
    if backend not in ("auto", "ragas"):
        raise ValueError(f"unknown backend {backend!r}")
    try:
        from datasets import Dataset
        from ragas import evaluate as ragas_evaluate
        from ragas import metrics as ragas_metrics
    except ImportError:
        if backend == "ragas":
            raise
        return {"backend": "unavailable",
                "message": "install the 'rag' extra to use RAGAS"}
    # RAGAS renamed/re-shaped metrics between 0.1 and 0.2: 0.1 exposes
    # lowercase singletons, 0.2 mainly exposes classes (Faithfulness, ...).
    # Accept both rather than pinning a call site to one release.
    aliases = {
        "faithfulness": ("faithfulness", "Faithfulness"),
        "answer_relevancy": ("answer_relevancy", "ResponseRelevancy", "AnswerRelevancy"),
        "context_precision": ("context_precision", "LLMContextPrecisionWithReference",
                              "ContextPrecision"),
    }
    registry = {}
    for name in metrics:
        value = None
        for candidate in aliases.get(name, (name,)):
            value = getattr(ragas_metrics, candidate, None)
            if value is not None:
                break
        if isinstance(value, type):
            value = value()
        if value is not None:
            registry[name] = value
    selected = [registry[name] for name in metrics if name in registry]
    dataset = Dataset.from_list(list(samples))
    kwargs = {"dataset": dataset, "metrics": selected}
    if llm is not None:
        kwargs["llm"] = llm
    score = ragas_evaluate(**kwargs)
    values = {name: round(float(value), 4) for name, value in score.items()}
    return {"backend": "ragas", "metrics": values,
            "judge": getattr(llm, "model_name", None) or str(llm) if llm else "ragas-default"}
