"""Offline evaluation for the ``contract-step`` llm-d-sc taxonomy.

The point of this module is to answer one question before the taxonomy is
wired into routing: does the classifier separate the four task instructions,
or is it just recognising the contract body that every prompt carries?

Case families:

``real-prompt``
    The exact user prompts produced by the pipeline's four builders
    (``classify_body``, ``extract_body``, ``risk_body``, ``report_body``).

``paraphrase``
    The same task content, written four other ways per label. These are the
    held-out cases: no anchor in the taxonomy is copied from them.

``same-document-control``
    One contract body under all four instructions. The expected label follows
    the instruction, so a classifier that keyed on the body instead of the
    task cannot pass this family.

``long-document``
    The same four-instruction control over an agreement larger than the
    filter's 4096-character prompt budget.

``agent-shaped``
    Tool-result-only and assistant-only requests with no user turn (expected
    to be unrouted), plus requests whose final user turn follows tool or
    assistant content (expected to classify that turn).

The module is transport-agnostic for construction and scoring. ``run_cases``
adds an OpenAI-compatible HTTP runner; it only reads routing headers, so it
works through the full stack or directly in front of a gateway.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

LABELS = ("CLASSIFY", "EXTRACT", "RISK", "REPORT")
# Expected value for an agent-shaped request with no final user message. The
# filter cannot extract a stage task, so the correct outcome is the default
# cluster, not an invented label.
UNROUTED = "UNROUTED"
DEFAULT_TAXONOMY = (Path(__file__).resolve().parents[4]
                    / "classifiers" / "contract-step.json")

# One document, deliberately realistic but free of personal data. The
# same-document control reuses it unchanged for every instruction.
CONTROL_DOC = """MASTER SERVICES AGREEMENT
Contract Reference: C-2026-0420
Effective Date: 2026-02-01
Initial Term: 24 months

1. Services. Supplier will provide the services described in each statement of work.
2. Termination. Either party may terminate this Agreement for convenience on ninety (90) days' prior written notice.
3. Governing Law. This Agreement is governed by the laws of Ireland.
4. Liability. Supplier's aggregate liability is capped at EUR 1,000,000.
5. Data Protection. Supplier will process personal data only as described in Schedule 3.
"""

CLAUSES = {
    "termination": (
        "Either party may terminate this Agreement for convenience on ninety "
        "(90) days' prior written notice."
    ),
    "governing_law": "This Agreement is governed by the laws of Ireland.",
    "liability_cap": "Supplier's aggregate liability is capped at EUR 1,000,000.",
    "data_protection": "Supplier will process personal data only as described in Schedule 3.",
}

DOC_LINES = [
    json.dumps({"ref": "C-2026-0420", "type": "MSA", "risk": "medium",
                "governing_law": "Ireland", "data_protection": True}),
    json.dumps({"ref": "C-2026-0421", "type": "NDA", "risk": "low",
                "governing_law": "Ireland", "data_protection": True}),
    json.dumps({"ref": "C-2026-0422", "type": "SOW", "risk": "high",
                "governing_law": "England and Wales", "data_protection": False}),
]

# A contract that exceeds the filter's default 4096-character prompt budget.
# The instruction is deliberately first, so truncation still leaves the task in
# the prompt. This is the realistic long-document case: the gateway classifies
# a prefix, not the whole agreement.
LONG_DOC = CONTROL_DOC + "\n\nADDITIONAL TERMS\n" + "\n".join(
    f"{index}. The parties agree that the obligations in this section apply "
    f"to the applicable statement of work and survive termination for a "
    f"period of {index % 5 + 1} years."
    for index in range(1, 101)
)


@dataclass(frozen=True)
class Case:
    """One classification request and the label its instruction implies."""

    case_id: str
    expected: str
    family: str
    step: str
    body: dict[str, Any]
    group: str | None = None
    note: str = ""
    long_document: bool = False


def _chat_body(prompt: str) -> dict[str, Any]:
    """A minimal OpenAI-shaped request with the pipeline's system prompt."""
    from ..pipeline.review import SYSTEM_PROMPT

    return {
        "model": "Semantic-Qwen",
        "temperature": 0,
        "max_tokens": 16,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }


def _paraphrase_prompts() -> dict[str, list[str]]:
    """Held-out task instructions, distinct from every production template."""
    return {
        "CLASSIFY": [
            "Tell me the agreement category and the length of the first term.\n\n"
            + CONTROL_DOC,
            "Which contract type is this, and when does the initial period end?\n\n"
            + CONTROL_DOC,
            "Read this instrument and say whether it is an MSA, NDA, SOW, or DPA; "
            "include the initial term.\n\n" + CONTROL_DOC,
            "What sort of contract am I looking at, and what is its opening duration?\n\n"
            + CONTROL_DOC,
        ],
        "EXTRACT": [
            "Find the termination provision in this agreement and quote it.\n\n"
            + CONTROL_DOC,
            "What is the contract's position on termination? Return the wording.\n\n"
            + CONTROL_DOC,
            "Locate the termination section and reproduce its text, or say it is not "
            "present.\n\n" + CONTROL_DOC,
            "Pull out the language about ending the agreement.\n\n" + CONTROL_DOC,
        ],
        "RISK": [
            "Given these extracted terms, how much legal exposure does the company "
            "carry?\n\nExtracted clauses: " + json.dumps(CLAUSES),
            "Is this contract low, medium, or high risk? Explain your rating.\n\n"
            "Extracted clauses: " + json.dumps(CLAUSES),
            "Review the obligations below and assign an overall danger level.\n\n"
            "Extracted clauses: " + json.dumps(CLAUSES),
            "What could go wrong under these terms, and how serious is it?\n\n"
            "Extracted clauses: " + json.dumps(CLAUSES),
        ],
        "REPORT": [
            "Prepare the management roll-up for the whole batch and identify the most "
            "dangerous agreement.\n\n" + "\n".join(f"- {line}" for line in DOC_LINES),
            "Across every document in this set, summarize the totals and ranking.\n\n"
            + "\n".join(f"- {line}" for line in DOC_LINES),
            "Turn these per-document results into one portfolio briefing with aggregate "
            "numbers.\n\n" + "\n".join(f"- {line}" for line in DOC_LINES),
            "Give me the cross-document summary: risk counts, law counts, and the top "
            "exposure.\n\n" + "\n".join(f"- {line}" for line in DOC_LINES),
        ],
    }


def _control_prompts() -> dict[str, str]:
    """One document, four instructions. The label must follow the instruction."""
    return {
        "CLASSIFY": (
            "Identify the contract type and initial term from the document below.\n\n"
            + CONTROL_DOC
        ),
        "EXTRACT": (
            "Return the termination clause from the document below.\n\n"
            + CONTROL_DOC
        ),
        "RISK": (
            "Rate the legal exposure of the document below as low, medium, or high.\n\n"
            + CONTROL_DOC
        ),
        "REPORT": (
            "Prepare a portfolio briefing across the document set using the document "
            "below.\n\n" + CONTROL_DOC
        ),
    }


def _long_prompts() -> dict[str, str]:
    """The same long document under four instructions, each over budget."""
    return {
        "CLASSIFY": (
            "Tell me what kind of agreement this is and how long its initial "
            "term lasts.\n\n" + LONG_DOC
        ),
        "EXTRACT": (
            "Find the termination language in this agreement and quote it "
            "exactly.\n\n" + LONG_DOC
        ),
        "RISK": (
            "Assess this contract's legal exposure and assign a low, medium, or "
            "high rating.\n\n" + LONG_DOC
        ),
        "REPORT": (
            "Summarize this document set into one portfolio briefing with "
            "aggregate risk counts.\n\n" + LONG_DOC
        ),
    }


def _agent_cases() -> list[Case]:
    """Requests shaped like an agent loop, including requests with no user turn.

    The filter classifies the last ``role: user`` message. Tool results and
    assistant turns are deliberately *not* promoted to the classification
    prompt. A request with no user turn must fall through to the default
    cluster rather than inventing a task label.
    """
    system = {"role": "system", "content": "You are a contract-analysis agent."}
    tool_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_read_contract",
            "type": "function",
            "function": {"name": "read_contract", "arguments": "{}"},
        }],
    }
    tool_result = {
        "role": "tool",
        "tool_call_id": "call_read_contract",
        "content": CONTROL_DOC,
    }
    assistant_text = {"role": "assistant", "content": CONTROL_DOC}

    def case(case_id: str, expected: str, messages: list[dict[str, Any]],
             note: str) -> Case:
        return Case(
            case_id,
            expected,
            "agent-shaped",
            "agent",
            {
                "model": "Semantic-Qwen",
                "temperature": 0,
                "max_tokens": 16,
                "messages": messages,
            },
            group="agent-shaped",
            note=note,
        )

    return [
        case("agent-tool-result-only", UNROUTED, [system, tool_call, tool_result],
             "tool result with contract text but no user turn"),
        case("agent-assistant-only", UNROUTED, [system, assistant_text],
             "assistant turn with contract text but no user turn"),
        case("agent-system-only", UNROUTED, [system],
             "no user turn at all"),
        case("agent-tool-then-user-risk", "RISK",
             [system, tool_call, tool_result,
              {"role": "user", "content":
               "Assess the legal exposure in the contract we just read."}],
             "last user instruction follows a tool result; it is the task"),
        case("agent-assistant-then-user-extract", "EXTRACT",
             [system, assistant_text,
              {"role": "user", "content":
               "Quote the termination clause from that contract exactly."}],
             "last user instruction follows an assistant turn; it is the task"),
    ]


def build_cases() -> list[Case]:
    """Build the complete evaluation set.

    The production builders are imported lazily so the case builder can be
    used without loading the rest of the pipeline's optional dependencies.
    """
    from ..pipeline.review import classify_body, extract_body, report_body, risk_body

    cases: list[Case] = []

    # 1. The exact pipeline prompts. These are the prompts the gateway will see
    #    in a real run, including the document or extracted clauses.
    real = [
        ("real-classify", "CLASSIFY", "classify", classify_body(CONTROL_DOC)),
        ("real-extract", "EXTRACT", "extract",
         extract_body(CONTROL_DOC, "termination")),
        ("real-risk", "RISK", "risk", risk_body(CLAUSES, False)),
        ("real-report", "REPORT", "report",
         report_body([f"- {line}" for line in DOC_LINES], len(DOC_LINES), True)),
    ]
    for case_id, label, step, body in real:
        cases.append(Case(case_id, label, "real-prompt", step, body,
                          group="real-prompt",
                          note="built by the production prompt builder"))

    # 2. Held-out paraphrases. The task content is held constant while the
    #    wording changes, so this measures instruction generalisation rather
    #    than exact-template matching.
    for label, prompts in _paraphrase_prompts().items():
        for index, prompt in enumerate(prompts, start=1):
            cases.append(Case(
                f"paraphrase-{label.lower()}-{index}",
                label,
                "paraphrase",
                label.lower(),
                _chat_body(prompt),
                group=f"paraphrase-{label.lower()}",
                note="held-out paraphrase; no taxonomy anchor is copied from it",
            ))

    # 3. Same document, four instructions. This is the control that makes the
    #    other two families meaningful: if the body dominates, all four cases
    #    collapse to one label and the taxonomy is not routing on the task.
    for label, prompt in _control_prompts().items():
        cases.append(Case(
            f"control-{label.lower()}",
            label,
            "same-document-control",
            label.lower(),
            _chat_body(prompt),
            group="same-document-control",
            note="same contract body under all four instructions",
        ))

    # 4. The same long agreement under all four instructions. The prompt is
    #    over the filter's truncation budget on purpose; the leading
    #    instruction must still determine the label. This is the long-document
    #    counterpart to the control, not a fourth copy of the short one.
    for label, prompt in _long_prompts().items():
        cases.append(Case(
            f"long-{label.lower()}",
            label,
            "long-document",
            label.lower(),
            _chat_body(prompt),
            group="long-document-control",
            note="same long contract body under all four instructions; "
                 "the filter sees the first 4096 characters",
            long_document=True,
        ))

    # 5. Agent-shaped requests. Three have no user turn and must not invent a
    #    label; two put the real task in the final user turn after tool or
    #    assistant content, and must classify that turn, not the payload.
    cases.extend(_agent_cases())

    return cases


def case_prompt(case: Case) -> str:
    """The text llm-d-sc will classify: the last user message."""
    users = [
        message.get("content", "")
        for message in case.body.get("messages", [])
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    return users[-1] if users else ""


def load_taxonomy(path: str | Path | None = None) -> dict[str, Any]:
    return json.loads(Path(path or DEFAULT_TAXONOMY).read_text())


def validate_taxonomy(taxonomy: dict[str, Any],
                      expected_labels: Iterable[str] = LABELS) -> list[str]:
    """Return a list of problems; empty means the definition is usable."""
    problems: list[str] = []
    labels = taxonomy.get("labels") or []
    if set(labels) != set(expected_labels):
        problems.append(f"labels are {labels}, expected {list(expected_labels)}")
    if not taxonomy.get("taxonomy_revision"):
        problems.append("taxonomy_revision is empty")
    if not taxonomy.get("signal"):
        problems.append("signal is empty")
    if int(taxonomy.get("top_k", 0)) < 1:
        problems.append("top_k must be at least 1")

    anchors = taxonomy.get("anchors") or {}
    seen: dict[str, str] = {}
    for label in labels:
        texts = anchors.get(label) or []
        if len(texts) < 5:
            problems.append(f"{label} has {len(texts)} anchors; use at least 5")
        for text in texts:
            key = " ".join(str(text).lower().split())
            if key in seen:
                problems.append(f"duplicate anchor under {label} and {seen[key]}: {text!r}")
            seen[key] = label
    for label in anchors:
        if label not in labels:
            problems.append(f"anchor group {label!r} is not a declared label")
    return problems


def anchor_leakage(cases: Iterable[Case], taxonomy: dict[str, Any],
                   min_chars: int = 20) -> list[dict[str, str]]:
    """Find anchors copied into (or containing) an evaluation prompt.

    A leak makes the evaluation circular: the classifier would be matching the
    test prompt's predecessor, not generalising to the task. The threshold
    avoids flagging short common phrases.
    """
    anchors = [
        text for texts in (taxonomy.get("anchors") or {}).values()
        for text in texts
        if len(str(text)) >= min_chars
    ]
    leaks: list[dict[str, str]] = []
    for case in cases:
        prompt = case_prompt(case).lower()
        for anchor in anchors:
            needle = str(anchor).lower()
            if needle in prompt or (len(prompt) >= min_chars and prompt in needle):
                leaks.append({"case_id": case.case_id, "anchor": str(anchor)})
    return leaks


def request_body(case: Case, max_tokens: int = 8) -> dict[str, Any]:
    """The case body with generation-cost knobs removed.

    The classifier runs before the model, so the evaluation only needs the
    prompt and routing headers. Keeping the upstream answer short makes the
    harness cheap against a real stack.
    """
    drop = {
        "response_format", "guided_json", "structured_outputs",
        "chat_template_kwargs",
    }
    body = {key: value for key, value in case.body.items() if key not in drop}
    body["max_tokens"] = max_tokens
    body["temperature"] = 0
    return body


@dataclass(frozen=True)
class Prediction:
    case_id: str
    label: str | None
    score: float | None
    route: str | None
    status: str | None = None
    error: str | None = None


def prediction_from_headers(case_id: str, headers: Any, *,
                            status: str | None = None,
                            error: str | None = None) -> Prediction:
    """Read llm-d-sc provenance from either the filter or the trace relay."""
    get = getattr(headers, "get", None)
    if get is None:
        raise TypeError("headers must provide .get(name)")

    def first(*names: str) -> str | None:
        for name in names:
            value = get(name)
            if value:
                return str(value)
        return None

    label = first("x-llm-d-sc-label", "x-demo-classification")
    score = first("x-llm-d-sc-score")
    try:
        parsed_score = float(score) if score is not None else None
    except ValueError:
        parsed_score = None
    return Prediction(
        case_id=case_id,
        label=label.upper() if label else None,
        score=parsed_score,
        route=first("x-demo-route", "x-llm-d-sc-cluster"),
        status=status or first("x-llm-d-sc-status"),
        error=error,
    )


def score_predictions(cases: Iterable[Case],
                      predictions: Iterable[Prediction]) -> dict[str, Any]:
    by_id = {prediction.case_id: prediction for prediction in predictions}
    confusion = {expected: {predicted: 0 for predicted in (*LABELS, "MISSING")}
                 for expected in LABELS}
    family = {}
    same_doc_total = same_doc_correct = 0
    long_doc_total = long_doc_correct = 0
    unrouted_total = unrouted_correct = 0
    correct = 0
    errors: list[dict[str, str]] = []

    for case in cases:
        prediction = by_id.get(case.case_id)
        predicted = prediction.label if prediction and prediction.label in LABELS else "MISSING"
        is_unrouted = case.expected == UNROUTED

        if is_unrouted:
            # Correct means the request succeeded and the filter emitted no
            # label. A failed request is not evidence that the skip path works.
            is_correct = (
                prediction is not None
                and prediction.error is None
                and predicted == "MISSING"
            )
            unrouted_total += 1
            unrouted_correct += int(is_correct)
        else:
            confusion[case.expected][predicted] += 1
            is_correct = predicted == case.expected
        correct += int(is_correct)

        bucket = family.setdefault(case.family, {"total": 0, "correct": 0})
        bucket["total"] += 1
        bucket["correct"] += int(is_correct)

        if case.family == "same-document-control":
            same_doc_total += 1
            same_doc_correct += int(is_correct)

        if case.family == "long-document":
            long_doc_total += 1
            long_doc_correct += int(is_correct)

        if is_unrouted and not is_correct:
            errors.append({
                "case_id": case.case_id,
                "expected": UNROUTED,
                "predicted": predicted,
                "status": prediction.status if prediction else None,
                "error": (prediction.error if prediction else
                          "no response; expected a successful skip"),
            })
        elif not is_unrouted and (
            prediction is None or prediction.error or predicted == "MISSING"
        ):
            errors.append({
                "case_id": case.case_id,
                "expected": case.expected,
                "predicted": predicted,
                "status": prediction.status if prediction else None,
                "error": (prediction.error if prediction else "no prediction"),
            })

    total = sum(bucket["total"] for bucket in family.values())
    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0.0,
        "confusion": confusion,
        "families": family,
        "same_document_control": {
            "correct": same_doc_correct,
            "total": same_doc_total,
            "accuracy": same_doc_correct / same_doc_total if same_doc_total else 0.0,
        },
        "long_document_control": {
            "correct": long_doc_correct,
            "total": long_doc_total,
            "accuracy": long_doc_correct / long_doc_total if long_doc_total else 0.0,
        },
        "agent_no_user": {
            "correct": unrouted_correct,
            "total": unrouted_total,
            "accuracy": unrouted_correct / unrouted_total if unrouted_total else 0.0,
        },
        "errors": errors,
    }


async def run_cases(base_url: str, cases: Iterable[Case], *,
                    concurrency: int = 8, timeout: float = 60.0,
                    token: str | None = None,
                    extra_headers: dict[str, str] | None = None) -> list[Prediction]:
    """Send cases to an OpenAI-compatible gateway and read routing headers."""
    cases = list(cases)
    headers = dict(extra_headers or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout,
                                 headers=headers) as client:
        async def one(case: Case) -> Prediction:
            async with semaphore:
                try:
                    response = await client.post("/v1/chat/completions",
                                                 json=request_body(case))
                except httpx.HTTPError as exc:
                    return Prediction(case.case_id, None, None, None,
                                      error=f"{type(exc).__name__}: {exc}")
                return prediction_from_headers(
                    case.case_id,
                    response.headers,
                    error=None if response.status_code < 400
                    else f"http_{response.status_code}",
                )

        return list(await asyncio.gather(*(one(case) for case in cases)))


def format_summary(result: dict[str, Any], *, title: str = "contract-step") -> str:
    lines = [
        f"{title} routing evaluation",
        f"cases: {result['total']}  correct: {result['correct']}  "
        f"accuracy: {result['accuracy']:.1%}",
        "",
        "Confusion matrix (rows expected, columns predicted):",
    ]
    header = " " * 12 + " ".join(f"{label:>9}" for label in (*LABELS, "MISSING"))
    lines.append(header)
    for expected in LABELS:
        row = result["confusion"][expected]
        lines.append(f"{expected:>12} " + " ".join(
            f"{row[label]:>9}" for label in (*LABELS, "MISSING")))
    lines.append("")
    lines.append("Per family:")
    for name, bucket in result["families"].items():
        accuracy = bucket["correct"] / bucket["total"] if bucket["total"] else 0.0
        lines.append(f"  {name:28} {bucket['correct']:>2}/{bucket['total']:<2} "
                     f"({accuracy:.1%})")
    control = result["same_document_control"]
    lines.append(f"  same-document control       {control['correct']:>2}/"
                 f"{control['total']:<2} ({control['accuracy']:.1%})")
    long_control = result["long_document_control"]
    lines.append(f"  long-document control       {long_control['correct']:>2}/"
                 f"{long_control['total']:<2} ({long_control['accuracy']:.1%})")
    no_user = result["agent_no_user"]
    lines.append(f"  no-user turns (must skip)   {no_user['correct']:>2}/"
                 f"{no_user['total']:<2} ({no_user['accuracy']:.1%})")
    if result["errors"]:
        lines.append("")
        lines.append("Missing or failed cases:")
        for error in result["errors"]:
            lines.append(f"  {error['case_id']}: expected {error['expected']}, "
                         f"got {error['predicted']} ({error['error']})")
    return "\n".join(lines)


def _write_cases(path: Path, cases: Iterable[Case]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for case in cases:
            handle.write(json.dumps({
                "case_id": case.case_id,
                "expected": case.expected,
                "family": case.family,
                "step": case.step,
                "group": case.group,
                "note": case.note,
                "long_document": case.long_document,
                "prompt": case_prompt(case),
                "body": case.body,
            }) + "\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=None,
                        help="gateway base URL, e.g. http://127.0.0.1:8080")
    parser.add_argument("--taxonomy", default=str(DEFAULT_TAXONOMY),
                        help="contract-step taxonomy JSON")
    parser.add_argument("--out", default="runs/contract-step/eval.json",
                        help="JSON report path")
    parser.add_argument("--cases-out", default=None,
                        help="write the exact cases for replay (JSONL)")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--token", default=os.environ.get("TRACE_API_TOKEN") or None)
    parser.add_argument("--header", action="append", default=[],
                        metavar="NAME=VALUE",
                        help="extra request header (repeatable)")
    parser.add_argument("--max-prompt-chars", type=int, default=4096,
                        help="flag prompts longer than the filter's default "
                             "truncation budget")
    parser.add_argument("--fail-under", type=float, default=None,
                        help="exit non-zero if overall accuracy is below this")
    parser.add_argument("--check", action="store_true",
                        help="validate taxonomy and cases without sending requests")
    args = parser.parse_args(argv)

    taxonomy = load_taxonomy(args.taxonomy)
    cases = build_cases()
    problems = validate_taxonomy(taxonomy)
    if problems:
        for problem in problems:
            print(f"taxonomy: {problem}", file=sys.stderr)
        raise SystemExit(2)

    leaks = anchor_leakage(cases, taxonomy)
    over_budget = [
        {"case_id": case.case_id, "chars": len(case_prompt(case))}
        for case in cases if len(case_prompt(case)) > args.max_prompt_chars
    ]
    # Long-document cases are deliberately over budget. Any other over-budget
    # prompt is an accident: the instruction may be cut off before the
    # classifier sees the task.
    unexpected_long = [
        entry for entry in over_budget
        if not next(case for case in cases
                    if case.case_id == entry["case_id"]).long_document
    ]
    long_cases = [
        {"case_id": case.case_id, "chars": len(case_prompt(case))}
        for case in cases if case.long_document
    ]

    if args.check:
        if args.cases_out:
            _write_cases(Path(args.cases_out), cases)
        result = {
            "taxonomy": taxonomy.get("classifier_id"),
            "taxonomy_revision": taxonomy.get("taxonomy_revision"),
            "cases": len(cases),
            "families": {name: sum(1 for case in cases if case.family == name)
                         for name in sorted({case.family for case in cases})},
            "anchor_leakage": leaks,
            "prompts_over_filter_budget": unexpected_long,
            "long_document_cases": long_cases,
        }
        if args.cases_out:
            result["cases_out"] = args.cases_out
        print(json.dumps(result, indent=2))
        return

    if not args.base_url:
        parser.error("--base-url is required unless --check is set")

    extra_headers = dict(
        item.split("=", 1) for item in args.header if "=" in item
    )
    cases = list(cases)
    predictions = asyncio.run(run_cases(
        args.base_url, cases, concurrency=args.concurrency,
        timeout=args.timeout, token=args.token, extra_headers=extra_headers,
    ))
    result = score_predictions(cases, predictions)
    result.update({
        "taxonomy": {
            "classifier_id": taxonomy.get("classifier_id"),
            "signal": taxonomy.get("signal"),
            "taxonomy_revision": taxonomy.get("taxonomy_revision"),
            "path": str(args.taxonomy),
        },
        "base_url": args.base_url,
        "anchor_leakage": leaks,
        "prompts_over_filter_budget": unexpected_long,
        "long_document_cases": long_cases,
    })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    if args.cases_out:
        _write_cases(Path(args.cases_out), cases)

    print(format_summary(result))
    if leaks:
        print(f"\nWARNING: {len(leaks)} taxonomy anchor(s) overlap a test prompt; "
              "the evaluation is partially circular.", file=sys.stderr)
    if unexpected_long:
        print(f"WARNING: {len(unexpected_long)} non-long-document prompt(s) "
              f"exceed the filter's {args.max_prompt_chars}-character budget "
              "and will be truncated.", file=sys.stderr)
    print(f"\nwrote {out}")
    if args.fail_under is not None and result["accuracy"] < args.fail_under:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
