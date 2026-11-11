"""Pluggable PII detection for the governance path.

The demo originally shipped a small regex screen in ``classifier.py``.  That
screen is *load-bearing*: it decides which gateway entry a document takes, so
a false negative is a containment failure and a false positive is unnecessary
GPU/sensitive-pool usage.  This module keeps the regex screen as the default
(zero new dependencies, byte-identical routing for the recorded arms) and adds
adapters that can be measured against it:

* ``presidio`` - Microsoft/community Presidio's analyser (NER + checksums +
  context), the industry default for PII detection.
* ``taxonomy`` - an HTTP bridge to llm-d-sc's built-in sensitivity taxonomy
  (PUBLIC/INTERNAL/CONFIDENTIAL/REGULATED/NEVER_EGRESS).  llm-d-sc speaks gRPC;
  the bridge is expected to expose a tiny JSON contract, documented below.
* ``jev`` - a zero-shot System One decision model (TypeSafe Jev, or the
  open-source Laya checkpoint) asked one document-level question.  It returns
  a calibrated probability, not spans, so its finding covers the whole text.
  No labels are used: this is the fit for real contracts, where the demo's
  regex has never been measured.
* ``screen`` - the same Jev decision served through the Praxis ``screen:pii``
  listener and the ``jev-cpu`` pool.  The pipeline calls the gateway before
  it chooses a review entry, so the screen request carries its own step
  header and API key; a gateway/serving failure fails closed to the sensitive
  entry.  Set ``HYBRID_PII_SCREEN_URL`` to the listener's System One path.

Selection is by argument or ``HYBRID_PII_DETECTOR`` (``regex`` default,
``presidio``, ``taxonomy``, ``jev``, ``screen``, or ``composite`` to OR the
first three).  The regex detector remains the measured default: swapping the
screen changes the sensitive share, the CPU-served share, and therefore the
arm numbers.  Run the comparison tool and re-measure before adopting.
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
KNOWN_DETECTORS = ("regex", "presidio", "taxonomy", "jev", "screen", "composite")
_FAILED_WARNED: set[str] = set()

# The question is deliberately about actual identifiers, not about the
# presence of a data-protection clause.  The demo's `sensitive` label is an
# actual PII schedule; 148/200 synthetic contracts carry a generic
# data-protection clause without being labelled sensitive.  Asking the model
# "is there a data-protection clause?" produced 72 false positives on that
# corpus; this wording produced none at the 0.5 threshold (FINDINGS §16.7).
JEV_PII_QUESTION = (
    "Does this text contain PII (personally identifiable information) such as "
    "an email address, phone number, employee ID, or a named individual's "
    "contact details?"
)
JEV_DEFAULT_THRESHOLD = 0.5
# ~350 English tokens per chunk: safe under the English checkpoint's 512-token
# window and cheap enough to scan a normal contract in a handful of calls.
JEV_CHUNK_CHARS = 1400
# A document longer than this is failed closed rather than silently sampled.
# 32 chunks is ~45k characters, far beyond the demo corpus.
JEV_MAX_CHUNKS = 32
_LAYA_ROUTER: object | None = None


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


def _jev_threshold(threshold: float | None = None) -> float:
    value = threshold if threshold is not None else os.environ.get(
        "HYBRID_JEV_THRESHOLD", str(JEV_DEFAULT_THRESHOLD))
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise DetectorUnavailable(f"invalid JEV threshold {value!r}") from error


def _jev_chunks(text: str) -> list[str]:
    """Split into bounded chunks, preferring a paragraph or sentence edge.

    The English checkpoint reads 512 tokens; long contracts must be scanned in
    pieces so the screen does not silently ignore the tail.  A small overlap
    keeps a sentence that straddles a boundary visible to both calls.
    """
    text = text or ""
    if len(text) <= JEV_CHUNK_CHARS:
        return [text] if text.strip() else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + JEV_CHUNK_CHARS, len(text))
        if end < len(text):
            floor = start + JEV_CHUNK_CHARS // 2
            boundary = max(text.rfind("\n", floor, end), text.rfind(". ", floor, end))
            if boundary != -1:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - 100, start + 1)
    if len(chunks) > JEV_MAX_CHUNKS:
        raise RuntimeError(
            f"text is {len(text)} chars, over the {JEV_MAX_CHUNKS}-chunk JEV "
            "limit; failing closed instead of sampling it"
        )
    return chunks


def _jev_probability(data: dict, question: str = "sensitive") -> float:
    """Read the ``noul`` probability from a System One answer."""
    answer = (data.get("answers") or {}).get(question) or {}
    for key in ("noul", "probability", "score"):
        if answer.get(key) is not None:
            return float(answer[key])
    for key in ("noul", "probability", "score"):
        if data.get(key) is not None:
            return float(data[key])
    raise ValueError(
        f"JEV response has no noul/probability for the {question!r} question: "
        f"{data!r}"
    )


def _laya_router():
    global _LAYA_ROUTER
    if _LAYA_ROUTER is not None:
        return _LAYA_ROUTER
    try:
        from laya import Router
    except ImportError as error:
        raise DetectorUnavailable(
            "laya is not installed; install the 'jev' extra "
            "(uv sync --extra jev) or set HYBRID_JEV_URL to a System One "
            "endpoint"
        ) from error
    _LAYA_ROUTER = Router(device=os.environ.get("HYBRID_JEV_DEVICE", "cpu"))
    return _LAYA_ROUTER


def _jev_http(chunk: str, url: str, timeout: float,
              instructions: str = JEV_PII_QUESTION,
              question: str = "sensitive",
              extra_headers: dict[str, str] | None = None,
              extra_body: dict | None = None) -> float:
    import httpx

    headers = dict(extra_headers or {})
    key = os.environ.get("HYBRID_JEV_API_KEY", "")
    if key:
        headers.setdefault("Authorization", f"Bearer {key}")
    body = {"state": chunk, "questions": {
        question: {"type": "noul", "instructions": instructions}}}
    body.update(extra_body or {})
    response = httpx.post(
        url,
        json=body,
        headers=headers or None,
        timeout=timeout,
    )
    response.raise_for_status()
    return _jev_probability(response.json(), question=question)


def _jev_local(chunk: str, model: str | None,
               instructions: str = JEV_PII_QUESTION,
               question: str = "sensitive") -> float:
    import laya

    router = _laya_router()
    model = model or os.environ.get("HYBRID_JEV_MODEL", "auto")
    if model == "auto":
        model = "english" if laya.is_english(chunk) else "multilingual"
    max_len = 8192 if model == "multilingual" else 512
    result = router.predict(
        chunk,
        {question: {"type": "noul", "instructions": instructions}},
        model=model,
        max_len=max_len,
    )
    return _jev_probability(result, question=question)


def jev_question_score(text: str, instructions: str, url: str | None = None,
                       model: str | None = None, timeout: float = 30.0,
                       question: str = "sensitive",
                       extra_headers: dict[str, str] | None = None,
                       extra_body: dict | None = None) -> float:
    """The maximum ``noul`` probability for an arbitrary System One question.

    ``jev_score`` is the PII convenience wrapper.  The cause-language trigger
    uses this function with a different question; keeping the chunking,
    endpoint selection and parsing in one place means both decisions observe
    exactly the same text boundaries.
    """
    endpoint = (url or os.environ.get("HYBRID_JEV_URL", "")).strip()
    best = 0.0
    for chunk in _jev_chunks(text):
        probability = (_jev_http(chunk, endpoint, timeout, instructions,
                                 question, extra_headers, extra_body)
                       if endpoint
                       else _jev_local(chunk, model, instructions, question))
        best = max(best, probability)
    return best


def jev_score(text: str, url: str | None = None, model: str | None = None,
              timeout: float = 30.0) -> float:
    """The maximum PII ``noul`` probability over the text's chunks.

    Exposed separately so an offline benchmark can sweep thresholds without
    re-running the model once per threshold.
    """
    return jev_question_score(text, JEV_PII_QUESTION, url=url, model=model,
                              timeout=timeout)


def jev_detect(text: str, url: str | None = None, threshold: float | None = None,
               model: str | None = None, timeout: float = 30.0) -> list[Finding]:
    """Zero-shot decision screen, in the System One request/answer shape.

    ``url`` (or ``HYBRID_JEV_URL``) selects a served endpoint; otherwise the
    open-source ``laya`` runtime is used locally.  The model answers one
    document-level question with a calibrated ``noul`` probability, so the
    returned finding covers the whole text and its ``score`` is that
    probability.  Any failure propagates to ``is_sensitive``, which fails
    closed.

    This is an A/B candidate, not the measured default.  On the 200-document
    synthetic corpus the regex screen is 41/0/159/0; the English Laya
    checkpoint at 0.5 is 40/0/159/1 (FINDINGS §16.7).  The synthetic corpus is
    the only labelled one; real contracts and CUAD have no PII labels.
    """
    cutoff = _jev_threshold(threshold)
    best = jev_score(text, url=url, model=model, timeout=timeout)
    if best < cutoff:
        return []
    return [Finding(0, len(text or ""), "PII", "jev", round(best, 4))]


SCREEN_DEFAULT_STEP = "screen:pii"
SCREEN_POLICY_MODEL = "Semantic-Qwen"


def screen_score(text: str, url: str | None = None, model: str | None = None,
                 timeout: float = 30.0) -> float:
    """Jev's PII probability served through the Praxis screen listener.

    The request is a normal System One body plus the two routing headers the
    gateway authenticates and routes on.  ``model`` is included only because
    the gateway's PPE policy routes are model-scoped; the Jev service ignores
    it.  An unconfigured or failing endpoint raises, so ``is_sensitive`` fails
    closed to the sensitive entry.
    """
    endpoint = (url or os.environ.get("HYBRID_PII_SCREEN_URL", "")).strip()
    if not endpoint:
        raise DetectorUnavailable(
            "no screen listener configured; set HYBRID_PII_SCREEN_URL to "
            "the Praxis screen listener's /v1/systemone path"
        )
    from .common import api_key_for_step

    step = os.environ.get("HYBRID_PII_SCREEN_STEP", SCREEN_DEFAULT_STEP)
    key = (os.environ.get("HYBRID_PII_SCREEN_KEY")
           or api_key_for_step("screen"))
    headers = {"X-Demo-Step": step, "X-API-Key": key}
    body = {"model": os.environ.get(
        "HYBRID_PII_SCREEN_POLICY_MODEL", SCREEN_POLICY_MODEL)}
    return jev_question_score(
        text, JEV_PII_QUESTION, url=endpoint, model=model, timeout=timeout,
        question="sensitive", extra_headers=headers, extra_body=body)


def screen_detect(text: str, url: str | None = None,
                  threshold: float | None = None, model: str | None = None,
                  timeout: float = 30.0) -> list[Finding]:
    """The ``screen`` detector: Jev behind Praxis, in the Finding shape.

    This is a serving-path adapter, not a different model.  The offline
    ``jev`` detector talks to a System One endpoint directly; ``screen`` adds
    the gateway step header, API key and policy model, so the call is
    authenticated, audited and routed to the ``jev-cpu`` pool before the
    pipeline chooses a review entry.
    """
    cutoff = _jev_threshold(threshold)
    best = screen_score(text, url=url, model=model, timeout=timeout)
    if best < cutoff:
        return []
    return [Finding(0, len(text or ""), "PII", "screen", round(best, 4))]


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
    if name == "jev":
        return jev_detect(text)
    if name == "screen":
        return screen_detect(text)
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
    try:
        import importlib.util

        laya = importlib.util.find_spec("laya") is not None
    except Exception:  # noqa: BLE001 - a broken install is "unavailable"
        laya = False
    jev = bool(os.environ.get("HYBRID_JEV_URL")) or laya
    screen = bool(os.environ.get("HYBRID_PII_SCREEN_URL"))
    return {"regex": True, "presidio": presidio, "taxonomy": taxonomy,
            "jev": jev, "screen": screen}


def compare(texts: Iterable[tuple[str, str | None]], detectors: Sequence[str] | None = None,
            truth: dict[str, bool] | None = None) -> dict:
    """Compare detectors over ``(doc_id, text)`` pairs.

    Returns per-detector document counts, total findings, and (when ``truth``
    is supplied) document-level TP/FP/TN/FN.  Truth is optional because CUAD
    ships no PII labels; the tool reports both views so a reviewer can see
    what is measurable and what is not.
    """
    # Jev is excluded from the implicit set: selecting it may download and
    # load a checkpoint, so a comparison must opt in with detectors=["jev"].
    names = list(detectors or [d for d in ("regex", "presidio", "taxonomy")
                               if available()[d]])
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
