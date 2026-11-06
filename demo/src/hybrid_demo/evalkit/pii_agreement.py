"""Agreement report: the gateway's shadow PII screen vs the pipeline's.

Since the PPE integration (image v0.9.1), the praxis policy layer runs an
observe-only PII scanner (``validator/gci-pii-observe``) on every
``cmf.llm_input``. It never denies and never rewrites; it logs one
structured JSON line per scanned request::

    {"plugin":"gci_pii_observe","session_id":"C-2026-0001",
     "subject":"gci-classify","matched":["EMAIL_ADDRESS"],
     "text_chars":8123}

This tool joins those lines — captured from the gateway's container logs
(``docker compose logs praxis`` / ``oc logs deploy/praxis``) — against a
pipeline run's ``results.json`` and reports whether the gateway-side screen
agrees with the client-side screen that actually routed the documents
(``demo/src/hybrid_demo/pii.py``, the DLP-style gate of design.md §3.1).

The comparison point is the **classify-time** observation: the classify
prompt is the document text plus fixed boilerplate, which is (modulo the
boilerplate) the same input the pipeline's screen sees. Later steps are
reported separately because their prompts are *expected* to contaminate the
scan — the risk prompt for a screened document literally contains "This
document contains personal data", and extract prompts embed clause quotes.
A disagreement at classify time is a finding about the screens; a
disagreement at risk time is a finding about the comparison, and the report
keeps them apart.

This is V19 groundwork: the number is the deliverable. Promoting the
scanner to a gate is a separate, measured decision (design.md §5, README
known gaps).
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

# serde_json's compact form; the scanner emits exactly this as the tracing
# message, but the host's log formatter may wrap it with timestamps and
# fields, so the line is located by marker and decoded from there.
OBSERVE_MARKER = '{"plugin":"gci_pii_observe"'
CLASSIFY_SUBJECT = "gci-classify"
SENSITIVE_ENTRIES = {"sensitive", "sensitive-judgment"}
_DECODER = json.JSONDecoder()


def parse_observe_lines(text: str) -> list[dict]:
    """Extract every observe record from a captured log, in order.

    Robust to the host's log formatting: the record is found by marker and
    decoded with ``raw_decode`` so anything the subscriber wraps around it
    (timestamps, levels, span fields) is ignored. Unparseable markers are
    skipped and counted by the caller via the returned-vs-matched
    difference — a truncated line must not kill the report.
    """
    records: list[dict] = []
    for match in re.finditer(re.escape(OBSERVE_MARKER), text):
        start = match.start()
        try:
            value, _ = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def load_run_records(run_dir: str | Path) -> list[dict]:
    path = Path(run_dir) / "results.json"
    data = json.loads(path.read_text())
    records = data.get("records", [])
    if not isinstance(records, list):
        raise SystemExit(f"FATAL: {path} has no records list")
    return records


def pipeline_screen_by_doc(records: list[dict]) -> dict[str | None, bool]:
    """The client-side screen's verdict per document, read from the entries
    the pipeline actually chose (arm D / hybrid: a screened document's
    steps carry a sensitive entry).

    The report step has no document (``doc_id`` is None) and is skipped;
    the screen is a per-document decision made before any step ran.
    """
    screened: dict[str | None, bool] = {}
    for record in records:
        doc_id = record.get("doc_id")
        if doc_id is None:
            continue
        entry = record.get("entry") or ""
        if entry in SENSITIVE_ENTRIES:
            screened[doc_id] = True
        elif doc_id not in screened:
            screened[doc_id] = False
    return screened


def gateway_flag_by_session(observe: list[dict], subject: str) -> dict[str, bool]:
    """The gateway scanner's verdict per session, restricted to one subject.

    ``subject`` selects which pipeline step's observations to read; the
    classify step is the apples-to-apples comparison, other steps are
    reported as context (their prompts embed document text plus
    instructions, so contamination is expected).
    """
    flagged: dict[str, bool] = {}
    for line in observe:
        if line.get("subject") != subject:
            continue
        session = line.get("session_id") or ""
        if not session:
            continue
        matched = bool(line.get("matched"))
        flagged[session] = flagged.get(session, False) or matched
    return flagged


def agreement(pipeline: dict[str, bool], gateway: dict[str, bool]) -> dict:
    """Confusion matrix of the gateway screen against the pipeline screen.

    The pipeline screen is the reference because it is the decision that
    routed the traffic; the question is whether the gateway would have made
    the same call.
    """
    tp = fp = tn = fn = 0
    for doc_id, wanted in pipeline.items():
        got = gateway.get(doc_id)
        if got is None:
            continue
        tp += int(got and wanted)
        fp += int(got and not wanted)
        tn += int(not got and not wanted)
        fn += int(not got and wanted)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {
        "compared": tp + fp + tn + fn,
        "pipeline_flagged": sum(1 for v in pipeline.values() if v),
        "gateway_flagged": sum(1 for v in gateway.values() if v),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "precision": None if precision is None else round(precision, 3),
        "recall": None if recall is None else round(recall, 3),
    }


def report(records: list[dict], observe: list[dict]) -> dict:
    pipeline = pipeline_screen_by_doc(records)
    classify = gateway_flag_by_session(observe, CLASSIFY_SUBJECT)

    per_subject: dict[str, dict] = {}
    for subject in sorted({str(line.get("subject") or "") for line in observe}):
        flags = gateway_flag_by_session(observe, subject)
        per_subject[subject] = {
            "observations": len(flags),
            "flagged": sum(1 for v in flags.values() if v),
        }

    entity_counts: Counter[str] = Counter()
    for line in observe:
        for entity in line.get("matched") or []:
            entity_counts[entity] += 1

    known_sessions = set(pipeline)
    unknown = sorted({s for s in classify if s not in known_sessions})
    no_coverage = sorted(d for d in pipeline if d not in classify)

    return {
        "documents": len(pipeline),
        "observe_lines": len(observe),
        "classify_time": agreement(pipeline, classify),
        # Context, not verdicts: later-step prompts embed document text and
        # instructions, so flag rates here include expected contamination
        # (the risk prompt for a screened doc says "contains personal data").
        "per_subject": per_subject,
        "entities": dict(entity_counts.most_common()),
        # Sessions the scanner saw that the run does not know (other
        # traffic through the gateway) and documents the scanner never
        # observed at classify time — the latter is the hook-firing
        # coverage check: a non-empty list means the scanner is not seeing
        # classify requests and the agreement numbers above are partial.
        "sessions_not_in_run": unknown,
        "documents_without_classify_observation": no_coverage,
    }


def format_summary(result: dict) -> str:
    classify = result["classify_time"]
    confusion = classify["confusion"]
    lines = [
        f"documents in run:        {result['documents']}",
        f"scanner observe lines:   {result['observe_lines']}",
        "classify-time agreement (the comparison that matters):",
        f"  compared:              {classify['compared']}",
        f"  pipeline flagged:      {classify['pipeline_flagged']}"
        f"   gateway flagged: {classify['gateway_flagged']}",
        f"  tp={confusion['tp']} fp={confusion['fp']}"
        f" tn={confusion['tn']} fn={confusion['fn']}",
        f"  precision={classify['precision']} recall={classify['recall']}",
        "per-subject flag rates (context; later steps embed doc text):",
    ]
    for subject, row in result["per_subject"].items():
        lines.append(f"  {subject:16} observations={row['observations']}"
                     f" flagged={row['flagged']}")
    lines.append(f"entities: {result['entities']}")
    if result["documents_without_classify_observation"]:
        lines.append(
            "WARNING: documents with no classify-time observation: "
            f"{len(result['documents_without_classify_observation'])} "
            "(scanner coverage is partial — check that cmf.llm_input fires)"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="gateway shadow PII screen vs the pipeline's screen")
    parser.add_argument("--run", required=True,
                        help="pipeline run directory (contains results.json)")
    parser.add_argument("--logs", required=True,
                        help="captured gateway logs (docker compose logs praxis "
                             "> logs.txt, or oc logs deploy/praxis)")
    parser.add_argument("--out", default=None,
                        help="write the full JSON report here")
    args = parser.parse_args(argv)

    records = load_run_records(args.run)
    observe = parse_observe_lines(Path(args.logs).read_text(errors="replace"))
    result = report(records, observe)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=1))
    print(format_summary(result))


if __name__ == "__main__":
    main()
