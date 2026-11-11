"""Offline benchmark for the Jev/Laya PII screen.

This is a *weights* measurement, not a serving measurement.  It runs the
screen over a labelled corpus on the local CPU, one document at a time, and
reports document-level precision/recall against the corpus's ``sensitive``
label.  It does not exercise Praxis, the gateway, the pools or the trace UI,
so it cannot support a throughput or cost claim.

The only labelled corpus in the repository is ``demo/data/synthetic`` (200
documents, 41 sensitive).  Its label is an actual PII schedule, not merely the
presence of a data-protection clause.  CUAD and real contracts have no PII
labels, which is why this is an offline candidate and not a measured arm.

Usage::

    cd demo
    uv run hybrid-jev-bench --data data/synthetic
    uv run hybrid-jev-bench --data data/synthetic --model english --json /tmp/jev.json
    uv run hybrid-jev-bench --data data/synthetic --url http://jev:8000/v1/systemone

The regex baseline is included by default so the result is read next to the
screen it would replace.
"""
from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from .. import pii

DEFAULT_THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)


def load_corpus(data_dir: str | Path) -> tuple[list[dict], dict[str, str]]:
    """Load ``ground_truth.json`` plus the document texts it names."""
    root = Path(data_dir)
    ground = json.loads((root / "ground_truth.json").read_text())
    documents = ground.get("documents") or []
    texts: dict[str, str] = {}
    for doc in documents:
        doc_id = doc["doc_id"]
        texts[doc_id] = (root / "documents" / f"{doc_id}.txt").read_text()
    return documents, texts


def confusion(rows: Iterable[tuple[bool, bool]]) -> dict:
    """Confusion matrix and the three ratios, with ``None`` for 0/0."""
    tp = fp = tn = fn = 0
    for got, want in rows:
        tp += int(got and want)
        fp += int(got and not want)
        tn += int(not got and not want)
        fn += int(not got and want)
    total = tp + fp + tn + fn
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "accuracy": round((tp + tn) / total, 3) if total else None,
        "flagged": tp + fp,
    }


def benchmark(data_dir: str | Path, detectors: Sequence[str] = ("regex", "jev"),
              thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
              model: str | None = None, url: str | None = None) -> dict:
    documents, texts = load_corpus(data_dir)
    truth = {doc["doc_id"]: bool(doc["sensitive"]) for doc in documents}
    result: dict = {
        "corpus": str(data_dir),
        "documents": len(documents),
        "sensitive": sum(truth.values()),
        "measurement": "offline static batch, local CPU; weights not serving",
        "model": model or ("served" if url else "auto"),
        "detectors": {},
    }
    for name in detectors:
        started = time.perf_counter()
        if name == "jev":
            scores = {doc_id: pii.jev_score(text, url=url, model=model)
                      for doc_id, text in texts.items()}
            sweeps = {}
            for cutoff in thresholds:
                sweeps[str(cutoff)] = confusion(
                    (scores[doc_id] >= cutoff, truth[doc_id]) for doc_id in texts)
            result["detectors"]["jev"] = {
                "seconds": round(time.perf_counter() - started, 2),
                "thresholds": sweeps,
                "scores": {doc_id: round(score, 4)
                           for doc_id, score in scores.items()},
            }
        else:
            flagged = {
                doc_id: bool(pii.detect(text, name))
                for doc_id, text in texts.items()
            }
            result["detectors"][name] = {
                "seconds": round(time.perf_counter() - started, 2),
                "confusion": confusion(
                    (flagged[doc_id], truth[doc_id]) for doc_id in texts),
            }
    return result


def format_table(result: dict) -> str:
    lines = [
        f"corpus: {result['corpus']}  "
        f"documents: {result['documents']}  sensitive: {result['sensitive']}",
        result["measurement"],
        "",
        f"{'detector':<10} {'threshold':>9} {'tp':>4} {'fp':>4} {'tn':>4} "
        f"{'fn':>4} {'precision':>9} {'recall':>7} {'accuracy':>8}",
    ]
    for name, row in result["detectors"].items():
        if "confusion" in row:
            lines.append(_format_row(name, "-", row["confusion"]))
            continue
        for cutoff, matrix in row["thresholds"].items():
            lines.append(_format_row(name, cutoff, matrix))
    return "\n".join(lines)


def _format_row(name: str, cutoff: str, matrix: dict) -> str:
    def show(value):
        return "-" if value is None else f"{value:.3f}"
    return (f"{name:<10} {cutoff:>9} {matrix['tp']:>4} {matrix['fp']:>4} "
            f"{matrix['tn']:>4} {matrix['fn']:>4} "
            f"{show(matrix['precision']):>9} {show(matrix['recall']):>7} "
            f"{show(matrix['accuracy']):>8}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default="data/synthetic",
                        help="corpus directory with ground_truth.json and documents/")
    parser.add_argument("--detectors", nargs="+", default=["regex", "jev"],
                        choices=["regex", "jev"], help="screens to compare")
    parser.add_argument("--thresholds", nargs="+", type=float,
                        default=list(DEFAULT_THRESHOLDS),
                        help="Jev decision thresholds to sweep")
    parser.add_argument("--model", choices=["auto", "english", "multilingual"],
                        help="Laya checkpoint (default: HYBRID_JEV_MODEL or auto)")
    parser.add_argument("--url", help="served System One endpoint (HYBRID_JEV_URL)")
    parser.add_argument("--json", type=Path, help="write the full result as JSON")
    args = parser.parse_args()

    result = benchmark(args.data, tuple(args.detectors), tuple(args.thresholds),
                       model=args.model, url=args.url)
    print(format_table(result))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2) + "\n")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
