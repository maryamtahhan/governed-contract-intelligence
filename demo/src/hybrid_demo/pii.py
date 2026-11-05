"""Pluggable PII detection for the governance path.

The demo originally shipped a small regex screen in ``classifier.py``.  That
screen is *load-bearing*: it decides which gateway entry a document takes, so
a false negative is a containment failure and a false positive is unnecessary
GPU/sensitive-pool usage.  This module keeps the regex screen as the default
(zero new dependencies, byte-identical routing for the recorded arms) and adds
two adapters that can be measured against it:

* ``presidio`` - Microsoft/community Presidio's analyser (NER + checksums +
  context), the industry default for PII detection.
* ``taxonomy`` - an HTTP bridge to llm-d-sc's built-in sensitivity taxonomy
  (PUBLIC/INTERNAL/CONFIDENTIAL/REGULATED/NEVER_EGRESS).  llm-d-sc speaks gRPC;
  the bridge is expected to expose a tiny JSON contract, documented below.

Selection is by argument or ``HYBRID_PII_DETECTOR`` (``regex`` default,
``presidio``, ``taxonomy``, or ``composite`` to OR them).  Every detector
returns character-offset findings, so a comparison can report per-entity
agreement rather than just document-level flags.

This is deliberately *not* wired into the request path by default.  Swapping
the screen changes the sensitive share, the CPU-served share, and therefore
the arm numbers; run the comparison tool and re-measure before adopting.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from functools import lru_cache

# The original demo patterns, moved here unchanged so the default path is
# byte-identical.  ``classifier.PII_PATTERNS`` re-exports this tuple.
PII_PATTERNS = [
    r"[\w.+-]+@[\w-]+\.[\w.]+",
    r"\bEMP-\d{4,}\b",
    r"\bACCT-\d{4,}\b",
    r"\bCUST-\d{4,}\b",
    r"\b[A-Z]{2}\d{2}[A-Z0-9]{8,}\b",
    r"personal data",
    r"data controller",
    r"data processor",
    r"special categories of personal data",
    r"passport",
    r"tax identification",
    r"identification number",
    r"\+\d{1,3}[\s-]\d",
]

# Entity names are only used for reporting and for Presidio's allow/deny
# lists; the routing decision is "any finding at all".
_REGEX_ENTITIES = [
    ("EMAIL_ADDRESS", PII_PATTERNS[0]),
    ("EMPLOYEE_ID", PII_PATTERNS[1]),
    ("ACCOUNT_ID", PII_PATTERNS[2]),
    ("CUSTOMER_ID", PII_PATTERNS[3]),
    ("NATIONAL_ID", PII_PATTERNS[4]),
    ("PERSONAL_DATA_MARKER", PII_PATTERNS[5]),
    ("DATA_CONTROLLER_MARKER", PII_PATTERNS[6]),
    ("DATA_PROCESSOR_MARKER", PII_PATTERNS[7]),
    ("SPECIAL_CATEGORY_MARKER", PII_PATTERNS[8]),
    ("PASSPORT_MARKER", PII_PATTERNS[9]),
    ("TAX_ID_MARKER", PII_PATTERNS[10]),
    ("IDENTIFICATION_MARKER", PII_PATTERNS[11]),
    ("PHONE_NUMBER", PII_PATTERNS[12]),
]
_REGEX_COMPILED = [(name, re.compile(pattern)) for name, pattern in _REGEX_ENTITIES]

DEFAULT_DETECTOR = "regex"
KNOWN_DETECTORS = ("regex", "presidio", "taxonomy", "composite")
_FAILED_WARNED: set[str] = set()


@dataclass(frozen=True)
class Finding:
    start: int
    end: int
    entity: str
    detector: str
    score: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class DetectorUnavailable(RuntimeError):
    """Raised when an optional detector's dependency is not installed."""


def regex_detect(text: str) -> list[Finding]:
    """The original screen, with offsets.

    The patterns are compiled exactly as the classifier used them (case-
    sensitive), so default routing is byte-identical.  Adding offsets does not
    change the any-match decision.
    """
    findings: list[Finding] = []
    for name, pattern in _REGEX_COMPILED:
        for match in pattern.finditer(text or ""):
            findings.append(Finding(match.start(), match.end(), name, "regex"))
    return findings


@lru_cache(maxsize=1)
def _presidio_engine():
    try:
        from presidio_analyzer import AnalyzerEngine
    except ImportError as error:  # pragma: no cover - exercised by the extra
        raise DetectorUnavailable(
            "presidio is not installed; install the 'pii' extra "
            "(uv sync --extra pii) or `pip install presidio-analyzer`"
        ) from error
    return AnalyzerEngine()


def presidio_detect(text: str, language: str = "en",
                    entities: Sequence[str] | None = None) -> list[Finding]:
    """Presidio's analyser, returned in this module's Finding shape.

    Presidio's default recognisers cover email/phone/credit-card/IBAN/SSN/
    passport plus NER for person/location/organisation.  On contracts that
    means it will also flag company names and addresses, which is exactly why
    the comparison must be per-corpus rather than assumed.
    """
    engine = _presidio_engine()
    results = engine.analyze(text=text or "", language=language,
                             entities=list(entities) if entities else None)
    return [Finding(r.start, r.end, r.entity_type, "presidio", float(r.score))
            for r in results]


def taxonomy_detect(text: str, url: str | None = None,
                    timeout: float = 10.0) -> list[Finding]:
    """Adapter for llm-d-sc's built-in sensitivity taxonomy.

    llm-d-sc's native interface is gRPC.  A bridge in front of it should
    expose ``POST <url>`` with ``{"text": "..."}`` and return one of:

        {"labels": ["REGULATED", ...]}
        {"findings": [{"start": 0, "end": 10, "entity": "REGULATED"}]}
        {"label": "CONFIDENTIAL", "sensitive": true}

    A finding is emitted for every non-PUBLIC/INTERNAL label.  This keeps the
    comparison in the same shape as the other detectors without pretending the
    gRPC runtime is HTTP.
    """
    url = url or os.environ.get("HYBRID_PII_TAXONOMY_URL", "")
    if not url:
        raise DetectorUnavailable(
            "no sensitivity-taxonomy bridge configured; set "
            "HYBRID_PII_TAXONOMY_URL or pass url="
        )
    import httpx

    response = httpx.post(url, json={"text": text or ""}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        data = {"findings": data}
    findings: list[Finding] = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict) or "start" not in item or "end" not in item:
            continue
        findings.append(Finding(int(item["start"]), int(item["end"]),
                                str(item.get("entity") or "REGULATED"),
                                "taxonomy", item.get("score")))
    if findings:
        return findings
    labels = data.get("labels")
    if labels is None and data.get("label"):
        labels = [data["label"]]
    for label in labels or []:
        if str(label).upper() in {"PUBLIC", "INTERNAL"}:
            continue
        findings.append(Finding(0, len(text or ""), str(label), "taxonomy"))
    if not findings and data.get("sensitive") is True:
        findings.append(Finding(0, len(text or ""), "SENSITIVE", "taxonomy"))
    return findings


def resolve_detector(detector: str | None = None) -> str:
    name = (detector or os.environ.get("HYBRID_PII_DETECTOR") or DEFAULT_DETECTOR).strip().lower()
    if name not in KNOWN_DETECTORS:
        raise ValueError(f"unknown PII detector {name!r}; choose {', '.join(KNOWN_DETECTORS)}")
    return name


def detect(text: str, detector: str | None = None) -> list[Finding]:
    """Run one detector (or the composite) and return findings."""
    name = resolve_detector(detector)
    if name == "regex":
        return regex_detect(text)
    if name == "presidio":
        return presidio_detect(text)
    if name == "taxonomy":
        return taxonomy_detect(text)
    # composite: OR the detectors, tolerating an unavailable optional one.
    findings: list[Finding] = list(regex_detect(text))
    for optional in (presidio_detect, taxonomy_detect):
        try:
            findings.extend(optional(text))
        except DetectorUnavailable:
            continue
        except Exception:  # noqa: BLE001 - a broken optional detector must not
            # take down the screen; is_sensitive() fails closed on this path.
            continue
    return findings


def is_sensitive(text: str, detector: str | None = None) -> bool:
    """The routing question: does this text contain any finding?"""
    name = resolve_detector(detector)  # a typo is a config error, not "sensitive"
    try:
        return bool(detect(text, name))
    except Exception as error:  # noqa: BLE001 - fail closed on any detector failure
        # If the configured screen cannot run, treat the text as sensitive so
        # it stays on approved pools.  This is the governance invariant; a
        # detector bug must not become a containment failure.  Make the
        # failure visible once instead of silently sending everything to the
        # approved pools.
        if name not in _FAILED_WARNED:
            import sys

            print(f"pii: detector {name!r} failed ({error}); failing closed "
                  f"(all documents treated as sensitive)", file=sys.stderr)
            _FAILED_WARNED.add(name)
        return True


def available() -> dict[str, bool]:
    """Which optional detectors can run in this environment."""
    try:
        _presidio_engine()
        presidio = True
    except Exception:  # noqa: BLE001 - import/model errors are all "unavailable"
        presidio = False
    taxonomy = bool(os.environ.get("HYBRID_PII_TAXONOMY_URL"))
    return {"regex": True, "presidio": presidio, "taxonomy": taxonomy}


def compare(texts: Iterable[tuple[str, str | None]], detectors: Sequence[str] | None = None,
            truth: dict[str, bool] | None = None) -> dict:
    """Compare detectors over ``(doc_id, text)`` pairs.

    Returns per-detector document counts, total findings, and (when ``truth``
    is supplied) document-level TP/FP/TN/FN.  Truth is optional because CUAD
    ships no PII labels; the tool reports both views so a reviewer can see
    what is measurable and what is not.
    """
    names = list(detectors or [d for d, ok in available().items() if ok])
    rows: dict[str, dict] = {name: {"documents_flagged": 0, "findings": 0,
                                    "per_doc": {}, "errors": 0} for name in names}
    truth = truth or {}
    documents = 0
    for doc_id, text in texts:
        documents += 1
        for name in names:
            try:
                findings = detect(text or "", name)
                flagged = bool(findings)
            except (DetectorUnavailable, ValueError, RuntimeError, OSError) as error:
                rows[name]["errors"] += 1
                rows[name]["per_doc"][doc_id] = {"error": str(error)}
                continue
            rows[name]["findings"] += len(findings)
            rows[name]["documents_flagged"] += int(flagged)
            entry = {"flagged": flagged, "findings": len(findings),
                     "entities": sorted({f.entity for f in findings})}
            if doc_id in truth:
                entry["truth"] = truth[doc_id]
                entry["correct"] = flagged == truth[doc_id]
            rows[name]["per_doc"][doc_id] = entry
    if truth:
        for row in rows.values():
            tp = fp = tn = fn = 0
            for entry in row["per_doc"].values():
                if "truth" not in entry:
                    continue
                got, want = entry["flagged"], entry["truth"]
                tp += int(got and want)
                fp += int(got and not want)
                tn += int(not got and not want)
                fn += int(not got and want)
            precision = tp / (tp + fp) if tp + fp else None
            recall = tp / (tp + fn) if tp + fn else None
            row["confusion"] = {"tp": tp, "fp": fp, "tn": tn, "fn": fn,
                                "precision": None if precision is None else round(precision, 3),
                                "recall": None if recall is None else round(recall, 3)}
    return {"detectors": names, "documents": documents, "results": rows}


def main() -> None:
    """Tiny CLI: ``python -m hybrid_demo.pii <file>...`` prints findings."""
    import sys

    detector = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in KNOWN_DETECTORS else None
    paths = sys.argv[2:] if detector else sys.argv[1:]
    if not paths:
        print(json.dumps({"available": available()}, indent=2))
        return
    for path in paths:
        text = open(path, encoding="utf-8", errors="replace").read()
        print(json.dumps({"path": path, "detector": resolve_detector(detector),
                          "findings": [f.as_dict() for f in detect(text, detector)]},
                         indent=2))


if __name__ == "__main__":
    main()
