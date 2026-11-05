"""Compare PII detectors before changing the governance screen.

CUAD ships no PII ground truth, so the useful output is not one accuracy
number.  Run this on the corpus you actually route and read both views:

* document-level flags and entity counts per detector (what changes)
* TP/FP/TN/FN where a label exists (the synthetic drills, or a labelled sample)

    cd demo && uv run python tools/compare_pii.py \
        --data data/synthetic --detectors regex,presidio
    cd demo && uv run python tools/compare_pii.py \
        --cuad data/cuad/cuad-main/data.zip --limit 20 --detectors regex,presidio

If Presidio's NER flags company names/addresses, the sensitive share will
jump; that is a routing change, not a bug.  Re-run the arms after adopting.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hybrid_demo.pii import compare


def load_data_dir(path: Path) -> tuple[list[tuple[str, str]], dict[str, bool]]:
    texts: list[tuple[str, str]] = []
    truth: dict[str, bool] = {}
    documents = path / "documents"
    if not documents.is_dir():
        raise SystemExit(f"{path}: no documents/ directory")
    manifest = path / "ground_truth.json"
    if manifest.exists():
        data = json.loads(manifest.read_text())
        for doc in data.get("documents", []):
            if isinstance(doc.get("sensitive"), bool):
                truth[doc["doc_id"]] = doc["sensitive"]
    for file in sorted(documents.glob("*.txt")):
        texts.append((file.stem, file.read_text(errors="replace")))
    return texts, truth


def load_cuad(zip_path: Path, limit: int) -> list[tuple[str, str]]:
    from hybrid_demo.pipeline.cuad import load_cuad_split

    documents = load_cuad_split(zip_path)
    texts = []
    for i, doc in enumerate(documents):
        if limit and i >= limit:
            break
        paragraphs = doc.get("paragraphs") or []
        if paragraphs:
            texts.append((doc.get("title") or f"cuad-{i}", paragraphs[0]["context"]))
    return texts


def main() -> None:
    parser = argparse.ArgumentParser(description="compare PII detectors")
    parser.add_argument("--data", default=None, help="demo-format data directory")
    parser.add_argument("--cuad", default=None, help="CUAD data.zip")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--detectors", default=None,
                        help="comma-separated subset; default: every available one")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    if not args.data and not args.cuad:
        raise SystemExit("provide --data or --cuad")

    texts: list[tuple[str, str]] = []
    truth: dict[str, bool] = {}
    if args.data:
        texts, truth = load_data_dir(Path(args.data))
    if args.cuad:
        texts += load_cuad(Path(args.cuad), args.limit)
    detectors = ([d.strip() for d in args.detectors.split(",") if d.strip()]
                 if args.detectors else None)
    result = compare(texts, detectors=detectors, truth=truth)
    result["note"] = ("CUAD has no PII labels; document-level TP/FP is only "
                      "reported where the manifest supplies a label. "
                      "A detector swap changes the sensitive share and must be "
                      "re-measured on the routing arms.")
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
