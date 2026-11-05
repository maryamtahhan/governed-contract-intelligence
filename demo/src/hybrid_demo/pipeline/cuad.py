"""Convert CUAD (Contract Understanding Atticus Dataset) into the demo format.

CUAD v1: 510 real commercial contracts from SEC EDGAR with expert annotations
for 41 clause categories, CC BY 4.0 (verify before use - V12). The dataset
ships as SQuAD-style JSON (CUADv1.json inside data.zip): each document is a
"paragraph" whose context is the full contract text and whose questions are
`Highlight the parts (if any) of this contract related to "<CATEGORY>" ...`.

The converter maps four of the 41 categories onto the demo's clause keys:

    termination      <- "Termination For Convenience"
    governing_law    <- "Governing Law"
    liability_cap    <- "Cap On Liability"
    audit_rights     <- "Audit Rights"

Known gap, stated rather than papered over: CUAD has no data-protection
category, so the DP clause of the demo scenario is not annotatable from this
source (that is why the plan pairs CUAD with synthetic PII documents and
EU/GDPR sources for DP checks). Contracts longer than --max-chars are skipped:
they would exceed the pools' context windows.

CUAD carries no personal-data annotations, so this converter makes no
sensitivity claim: ``sensitive`` is ``None`` and the screen's accuracy is not
estimated on CUAD. Measuring PII recall needs an independently labelled set;
using the pipeline's own screen as truth would be circular.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import zipfile
from pathlib import Path

from .risk import assess_risk

CATEGORY_MAP = {
    "termination": "Termination For Convenience",
    "governing_law": "Governing Law",
    "liability_cap": "Cap On Liability",
    "audit_rights": "Audit Rights",
}
CLAUSE_KEYS = list(CATEGORY_MAP)
UNCAPPED = "Uncapped Liability"
QUESTION_RE = re.compile(r'related to "(.+?)" that should be reviewed')


def load_cuad_split(cuad_dir: str | Path, split: str | None = None) -> list[dict]:
    """All docs, or only the held-out test split when split='test'.

    The specialist checkpoints were fine-tuned on CUAD's train split, so any
    comparison against them must be restricted to test.json to stay honest.
    """
    if not split:
        return load_cuad_json(cuad_dir)
    cuad = Path(cuad_dir)
    for candidate in [cuad / "cuad" / "data.zip", cuad / "cuad-main" / "data.zip"]:
        if candidate.exists():
            import zipfile

            with zipfile.ZipFile(candidate) as zf:
                names = zf.namelist()
                split_file = next((n for n in names if n.endswith(f"{split}.json")), None)
                if split_file:
                    titles = {d["title"] for d in json.loads(zf.read(split_file))["data"]}
                    return [d for d in load_cuad_json(cuad_dir) if d["title"] in titles]
    raise SystemExit(f"split '{split}' not found under {cuad_dir} (need data.zip with {split}.json)")


def load_cuad_json(cuad_dir: str | Path) -> list[dict]:
    cuad = Path(cuad_dir)
    candidates = [cuad / "CUADv1.json", cuad / "CUAD_v1" / "CUADv1.json",
                  cuad / "cuad" / "data.zip", cuad / "cuad-main" / "data.zip"]
    for path in candidates:
        if path.suffix == ".zip" and path.exists():
            with zipfile.ZipFile(path) as zf:
                return json.loads(zf.read("CUADv1.json"))["data"]
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8", errors="replace"))["data"]
    raise SystemExit(f"no CUADv1.json or data.zip under {cuad_dir} (run scripts/data/fetch-cuad.sh)")


def answer_text(answers: list) -> str | None:
    for answer in answers or []:
        text = re.sub(r"\s+", " ", answer.get("text", "")).strip()
        if text:
            return text
    return None


def doc_id_for(title: str, used: set[str]) -> str:
    """A stable, unique doc_id for one CUAD title.

    Titles are truncated to keep filenames short, and two contracts can share
    the first 48 characters. Without the suffix guard the second one rewrote
    the first one's text file, so a conversion could silently drop a document
    while its manifest entry survived.
    """
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", title)[:48]
    doc_id, n = base, 2
    while doc_id in used:
        suffix = f"-{n}"
        doc_id = base[:48 - len(suffix)] + suffix
        n += 1
    used.add(doc_id)
    return doc_id


def risk_of(clauses: dict, uncapped: bool) -> str:
    """CUAD truth under the shared rubric; CUAD annotates no DP category."""
    return assess_risk(clauses, has_data_protection=False, uncapped=uncapped)


def convert(cuad_dir: str | Path, out_dir: str | Path, count: int, seed: int,
            max_chars: int = 24000, split: str | None = None) -> dict:
    data = load_cuad_split(cuad_dir, split)
    rng = random.Random(seed)
    rng.shuffle(data)
    out = Path(out_dir)
    docs_dir = out / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)

    wanted = {category: key for key, category in CATEGORY_MAP.items()}
    truths = []
    used_ids: set[str] = set()
    skipped_long = 0
    for doc in data:
        if len(truths) >= count:
            break
        para = doc["paragraphs"][0]
        text = para["context"]
        if len(text) < 400:
            continue
        if len(text) > max_chars:
            skipped_long += 1
            continue
        clauses: dict[str, str | None] = {key: None for key in CLAUSE_KEYS}
        uncapped = False
        for qa in para.get("qas", []):
            m = QUESTION_RE.search(qa.get("question", ""))
            if not m:
                continue
            category = m.group(1)
            if category == UNCAPPED and answer_text(qa.get("answers")):
                uncapped = True
            key = wanted.get(category)
            if key and clauses[key] is None:
                clauses[key] = answer_text(qa.get("answers"))
        doc_id = doc_id_for(doc["title"], used_ids)
        truths.append({
            "doc_id": doc_id,
            "type": None,
            "sensitive": None,
            "sensitivity_source": "unannotated (CUAD has no PII labels)",
            "initial_term_months": None,
            "clauses": clauses,
            "risk": risk_of(clauses, uncapped),
            "source": "cuad",
            "licence": "CC BY 4.0 (verify)",
        })
        (docs_dir / f"{doc_id}.txt").write_text(text, encoding="utf-8")

    manifest = {
        "seed": seed,
        "count": len(truths),
        "clause_keys": CLAUSE_KEYS,
        "golden": [t["doc_id"] for t in truths[:20]],
        "documents": truths,
        "source": "CUAD v1 (CC BY 4.0, verify before use)",
    }
    (out / "ground_truth.json").write_text(json.dumps(manifest, indent=1))
    return {
        "count": len(truths),
        "skipped_long": skipped_long,
        "sensitivity_annotated": False,
        "with_all_four_clauses": sum(1 for t in truths if all(t["clauses"].values())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="convert CUAD to the demo format")
    parser.add_argument("--cuad-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-chars", type=int, default=24000,
                        help="skip contracts longer than this (context-window budget)")
    parser.add_argument("--split", default=None, choices=[None, "test"],
                        help="restrict to the held-out test split (required for "
                             "honest comparison with the fine-tuned specialists)")
    args = parser.parse_args()
    print(json.dumps(convert(args.cuad_dir, args.out, args.count, args.seed, args.max_chars, args.split), indent=1))


if __name__ == "__main__":
    main()
