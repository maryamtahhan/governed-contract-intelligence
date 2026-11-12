"""hybrid-pii-agreement: joining the gateway's PII scanner lines to a run.

The scanner emits compact serde_json lines wrapped in whatever the host's
log formatter adds; the parser must survive that wrapping, and the report
must keep classify-time agreement (the comparison that matters) apart from
later-step context (whose prompts embed document text and are expected to
contaminate the scan).
"""
from __future__ import annotations

import json

from hybrid_demo.evalkit.pii_agreement import (
    agreement,
    parse_observe_lines,
    pipeline_screen_by_doc,
    report,
)


def _line(session: str, subject: str, matched: list[str], chars: int,
          *, plugin: str = "gci_pii_observe", action: str = "observe") -> str:
    """One log line as the host formatter wraps it: timestamp, level,
    target, then the scanner's compact serde_json record verbatim."""
    record = json.dumps(
        {"plugin": plugin, "action": action, "session_id": session,
         "subject": subject, "step": subject.removeprefix("gci-"),
         "matched": matched, "text_chars": chars},
        separators=(",", ":"))
    return f"2026-11-06T10:00:00Z INFO {plugin}: {record}"


LOGS = "\n".join([
    # Doc 1: screened sensitive by the pipeline; gateway agrees at classify.
    _line("C-2026-0001", "gci-classify", ["EMAIL_ADDRESS"], 8123),
    _line("C-2026-0001", "gci-extract", [], 9100),
    # Doc 2: not screened; gateway also clean at classify. The risk-step
    # marker is the prompt contamination the report must NOT count as a
    # classify-time disagreement.
    _line("C-2026-0002", "gci-classify", [], 7001),
    _line("C-2026-0002", "gci-risk", ["PERSONAL_DATA_MARKER"], 540),
    # Doc 3: not screened by the pipeline; the gateway flags it at classify
    # time — a genuine disagreement (fp from the pipeline's perspective).
    _line("C-2026-0003", "gci-classify", ["EMPLOYEE_ID"], 6600),
    # A truncated marker from an unrelated line: skipped, not fatal.
    '2026-11-06T10:00:05Z INFO other_target: broken {"plugin":"gci_pii_observe","session_id":"trunc',
    "",
])

RECORDS = [
    {"doc_id": "C-2026-0001", "step": "classify", "entry": "sensitive",
     "true_sensitive": True},
    {"doc_id": "C-2026-0001", "step": "extract:MSA-1.1", "entry": "sensitive",
     "true_sensitive": True},
    {"doc_id": "C-2026-0001", "step": "risk", "entry": "sensitive-judgment",
     "true_sensitive": True},
    {"doc_id": "C-2026-0002", "step": "classify", "entry": "general",
     "true_sensitive": False},
    {"doc_id": "C-2026-0002", "step": "risk", "entry": "judgment",
     "true_sensitive": False},
    {"doc_id": "C-2026-0003", "step": "classify", "entry": "general",
     "true_sensitive": False},
    # The report step has no document and must not appear in per-doc counts.
    {"doc_id": None, "step": "report", "entry": "judgment",
     "true_sensitive": False},
]


def test_observe_lines_parse_through_log_wrapping():
    lines = parse_observe_lines(LOGS)
    assert len(lines) == 5  # the truncated marker is skipped, not fatal
    assert lines[0]["session_id"] == "C-2026-0001"
    assert lines[0]["matched"] == ["EMAIL_ADDRESS"]
    assert lines[3]["matched"] == ["PERSONAL_DATA_MARKER"]


def test_gate_lines_parse_and_keep_their_action():
    line = _line("C-2026-0099", "gci-classify", ["EMAIL_ADDRESS"], 8123,
                 plugin="gci_pii_gate", action="deny")
    parsed = parse_observe_lines(line)
    assert parsed == [{
        "plugin": "gci_pii_gate", "action": "deny",
        "session_id": "C-2026-0099", "subject": "gci-classify",
        "step": "classify", "matched": ["EMAIL_ADDRESS"],
        "text_chars": 8123,
    }]


def test_observe_lines_parse_json_log_mode():
    # PRAXIS_LOG_FORMAT=json escapes the message inside a JSON log line;
    # the parser must recover the record from the message field.
    plain = parse_observe_lines(LOGS)
    json_logs = "\n".join(
        json.dumps({"timestamp": "2026-11-06T10:00:00Z", "level": "INFO",
                    "target": "gci_pii_observe", "message": _line(
                        "C-2026-0001", "gci-classify", ["EMAIL_ADDRESS"], 8123)
                    .split(": ", 1)[1]})
        for _ in range(1))
    parsed = parse_observe_lines(json_logs)
    assert parsed == [plain[0]]


def test_pipeline_screen_reads_entries_not_truth():
    screened = pipeline_screen_by_doc(RECORDS)
    assert screened == {"C-2026-0001": True,
                        "C-2026-0002": False,
                        "C-2026-0003": False}


def test_report_separates_classify_agreement_from_context():
    result = report(RECORDS, parse_observe_lines(LOGS))
    classify = result["classify_time"]
    # All three documents have classify-time observations: one tp (doc 1),
    # one tn (doc 2), one fp (doc 3) — and zero fn.
    assert classify["confusion"] == {"tp": 1, "fp": 1, "tn": 1, "fn": 0}
    assert classify["precision"] == 0.5
    assert classify["recall"] == 1.0
    # The risk-time contamination shows up as context, not in the matrix.
    assert result["per_subject"]["gci-risk"] == {"observations": 1, "flagged": 1}
    assert result["per_subject"]["gci-classify"] == {"observations": 3, "flagged": 2}
    # Entity counts cover every observed match across all steps.
    assert result["entities"] == {"EMAIL_ADDRESS": 1,
                                  "PERSONAL_DATA_MARKER": 1,
                                  "EMPLOYEE_ID": 1}
    assert result["documents_without_classify_observation"] == []


def test_missing_coverage_is_called_out():
    # A scanner that never fires: every document lacks a classify-time
    # observation, and the report must say so rather than quietly report
    # an empty matrix.
    result = report(RECORDS, [])
    assert result["classify_time"]["compared"] == 0
    assert result["documents_without_classify_observation"] == [
        "C-2026-0001", "C-2026-0002", "C-2026-0003"]


def test_agreement_skips_sessions_not_in_the_run():
    # Two extra gateway sessions the run knows nothing about: they must not
    # land in the confusion matrix.
    pipeline = {"doc-a": True, "doc-b": False}
    gateway = {"doc-a": True, "doc-b": False, "other-1": True, "other-2": False}
    result = agreement(pipeline, gateway)
    assert result["compared"] == 2
    assert result["confusion"] == {"tp": 1, "fp": 0, "tn": 1, "fn": 0}
