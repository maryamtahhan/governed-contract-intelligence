from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx

from ..common import normalize_text
from ..configs import name_for_key
from ..routes import APPROVED_ROUTES, CPU_ROUTES, GPU_ROUTES
from . import cost as cost_model
from . import spans as span_scoring
from .spans import text_matches

# Extraction is scored by containment, not span-equality: a prediction is
# correct when either normalised string contains the other. This rewards a
# wider quote that includes the annotated clause and penalises a correct
# paraphrase. Every report must name the metric; do not call it "exact".
# ``text_matches`` above is spans.py's containment default, re-exported
# here because retrieval/endtoend.py and the report import it from score.
MATCH_METRIC = "containment"

# Risk and report are the judgment stages. Termination extraction is still
# extraction (the CPU specialist asks a cause follow-up) and must not inflate
# this count in the report.
JUDGMENT_STEPS = {"risk", "report"}


# The report's governing-law counts name jurisdictions, but ground truth stores
# the whole annotated clause ("...the laws of the State of New York"). Counting
# those keys directly made the check unwinnable: every document produced a
# distinct key and a model answering "New York" could never match. Both sides
# are reduced to the jurisdiction they name before counting. Longest names
# match first so "West Virginia" and "Indiana" are not read as "Virginia" and
# "India".
_JURISDICTIONS = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "district of columbia", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "england and wales", "england", "scotland", "northern ireland", "ireland",
    "hong kong", "singapore", "australia", "new zealand", "canada", "ontario",
    "quebec", "india", "japan", "china", "germany", "france", "netherlands",
    "switzerland", "south africa", "united kingdom",
)
_JURISDICTIONS_BY_LENGTH = sorted(_JURISDICTIONS, key=len, reverse=True)


def jurisdiction(value: str | None) -> str | None:
    """The jurisdiction a clause or a model's answer names, if any."""
    if not value:
        return None
    low = normalize_text(str(value))
    return next((name for name in _JURISDICTIONS_BY_LENGTH if name in low), None)


def majority_risk_baseline(truth_docs: dict[str, dict], doc_ids: set[str]) -> float | None:
    """Always-answer-the-most-common-risk-level accuracy over the reviewed docs."""
    if not doc_ids:
        return None
    from collections import Counter

    counts = Counter(truth_docs[d].get("risk", "low") for d in doc_ids if d in truth_docs)
    if not counts:
        return None
    return round(max(counts.values()) / sum(counts.values()), 3)


def load_run(run_dir: str | Path) -> dict:
    data = json.loads((Path(run_dir) / "results.json").read_text())
    data["dir"] = str(run_dir)
    return data


def extraction_scores(records: list[dict], truth_docs: dict[str, dict],
                      doc_texts: dict[str, str] | None = None) -> dict:
    tp = fp = fn = 0
    correct_fields = 0
    total_fields = 0
    for r in records:
        if not r["step"].startswith("extract:"):
            continue
        clause = r["step"].split(":", 1)[1]
        truth = (truth_docs.get(r["doc_id"], {}).get("clauses") or {}).get(clause)
        content = r.get("content") or {}
        found = bool(content.get("found"))
        text = content.get("text")
        # A call that never returned cannot have answered "no clause". It is
        # neither a correct absence nor a false positive — it is a field the
        # run failed to fill, and it must not lift precision by default.
        served = r.get("outcome", "served") == "served"
        total_fields += 1
        if truth is None:
            if not served:
                continue
            if not found:
                correct_fields += 1
            else:
                fp += 1
        else:
            if found and served and text_matches(text, truth):
                tp += 1
                correct_fields += 1
            elif found:
                fp += 1
            else:
                fn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    # Additive views: the historical containment numbers above stay exactly
    # where they were, so old reports remain comparable.  Exact/partial and
    # nervaluate's offset strategies are extra evidence, not a replacement.
    views = span_scoring.score_records(records, truth_docs, doc_texts)
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "fields_correct": correct_fields,
        "fields_total": total_fields,
        "match_metric": MATCH_METRIC,
        "by_mode": views["by_mode"],
        "absence": views["absence"],
        "offset": views["offset"],
        "offset_backend": views["offset_backend"],
    }


def step_accuracy(records: list[dict], truth_docs: dict[str, dict], step: str,
                  field: str) -> float | None:
    hits = misses = 0
    for r in records:
        if r["step"] != step:
            continue
        truth = truth_docs.get(r["doc_id"], {})
        content = r.get("content") or {}
        if step == "classify":
            expected = truth.get("type")
            got = content.get("type")
        elif step == "risk":
            expected = truth.get("risk")
            got = content.get("risk")
        else:
            expected, got = truth.get(field), content.get(field)
        if expected is None:
            continue
        hits += int(got == expected)
        misses += int(got != expected)
    return round(hits / (hits + misses), 3) if hits + misses else None


def _law_counts(predicted: Any) -> dict[str, int]:
    """A report's governing-law counts, keyed by jurisdiction.

    Models answer with law names ("New York"); merging them after the same
    normalisation as the truth lets the two distributions be compared. Keys
    that name no known jurisdiction stay as written, so they still count
    against the answer.
    """
    counts: dict[str, int] = {}
    if not isinstance(predicted, dict):
        return counts
    for key, value in predicted.items():
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        name = jurisdiction(str(key)) or normalize_text(str(key))
        counts[name] = counts.get(name, 0) + count
    return counts


def _count_closeness(predicted: Any, truth: dict[str, int]) -> float:
    """Per-key closeness of two count dicts (1.0 = exact), forgiving minor
    propagation noise: one mis-extracted document shifts a count by one."""
    if not isinstance(predicted, dict):
        return 0.0
    keys = set(truth) | {str(k) for k in predicted}
    total = max(sum(truth.values()), 1)
    error = 0
    for key in keys:
        got = predicted.get(key, predicted.get(normalize_text(str(key)), 0))
        if not isinstance(got, int):
            try:
                got = int(got)
            except (TypeError, ValueError):
                got = 0
        want = truth.get(key, truth.get(normalize_text(str(key)), 0)) or 0
        error += abs(got - want)
    return max(0.0, 1.0 - error / total)


def report_checks(records: list[dict], truth_docs: dict[str, dict]) -> dict | None:
    """Per-field report results, against the ground truth for the run's documents.

    The blended score hides which field failed; this keeps the breakdown so a
    report violation can be attributed (documents, risk counts, governing law,
    highest-risk reference) instead of guessed at.
    """
    recs = [r for r in records if r["step"] == "report"]
    if not recs:
        return None
    content = recs[-1].get("content", {})
    if not content or "documents" not in content:
        return {"score": 0.0, "checks": {}, "predicted": content, "expected": {}}
    run_doc_ids = {r["doc_id"] for r in records if r["doc_id"]}
    docs = [truth_docs[d] for d in sorted(run_doc_ids) if d in truth_docs]
    risk_counts = {"low": 0, "medium": 0, "high": 0}
    law_counts: dict[str, int] = {}
    missing_dp = 0
    has_data_protection = any(
        "data_protection" in (d.get("clauses") or {}) for d in docs
    )
    for d in docs:
        risk_counts[d.get("risk", "low")] = risk_counts.get(d.get("risk", "low"), 0) + 1
        clause = d.get("clauses", {}).get("governing_law")
        law = jurisdiction(clause) or ("missing" if not clause else normalize_text(clause))
        law_counts[law] = law_counts.get(law, 0) + 1
        if not d.get("clauses", {}).get("data_protection"):
            missing_dp += 1
    # More than one contract can truthfully be "the highest risk" (CUAD marks
    # 13 of these 19 high), so any reference to a truth-high document counts.
    # Picking one by doc_id made the check depend on alphabetical order.
    high_ids = sorted(d["doc_id"] for d in docs if d.get("risk") == "high")
    highest = high_ids[0] if high_ids else None
    predicted_law_counts = _law_counts(content.get("governing_law_counts"))
    checks = {
        "documents": 1.0 if content.get("documents") == len(docs) else 0.0,
        "risk_counts": _count_closeness(content.get("risk_counts"), risk_counts),
        "highest_risk_ref": 1.0 if content.get("highest_risk_ref") in high_ids else 0.0,
        "governing_law_counts": _count_closeness(predicted_law_counts, law_counts),
    }
    # CUAD does not annotate a data-protection category; scoring a count the
    # prompt never requested would be a category error.
    if has_data_protection:
        checks["missing_data_protection"] = (
            1.0 if content.get("missing_data_protection") == missing_dp else 0.0
        )
    expected = {
        "documents": len(docs),
        "risk_counts": risk_counts,
        "governing_law_counts": law_counts,
        "highest_risk_ref": highest,
        "high_risk_count": len(high_ids),
    }
    if has_data_protection:
        expected["missing_data_protection"] = missing_dp
    return {
        "score": round(sum(checks.values()) / len(checks), 3),
        "checks": checks,
        "predicted": content,
        # Same normalisation as the truth, so a viewer can put the two count
        # dicts side by side without reimplementing jurisdiction matching.
        "predicted_governing_law_counts": predicted_law_counts,
        "expected": expected,
    }


def report_score(records: list[dict], truth_docs: dict[str, dict]) -> float | None:
    result = report_checks(records, truth_docs)
    return None if result is None else result["score"]


def docs_fully_correct(records: list[dict], truth_docs: dict[str, dict]) -> int:
    by_doc: dict[str, list[dict]] = {}
    for r in records:
        if r["doc_id"]:
            by_doc.setdefault(r["doc_id"], []).append(r)
    correct = 0
    for doc_id, recs in by_doc.items():
        truth = truth_docs.get(doc_id, {})
        ok = True
        for r in recs:
            clause = r["step"].split(":", 1)[1] if r["step"].startswith("extract:") else None
            # Same rule as the per-field scorer: a failed call has no answer,
            # so it cannot complete a document, even when the annotation says
            # the right answer was "no clause".
            served = r.get("outcome", "served") == "served"
            if clause:
                expected = (truth.get("clauses") or {}).get(clause)
                content = r.get("content") or {}
                if expected is None:
                    ok = ok and served and not content.get("found")
                else:
                    ok = (ok and served and bool(content.get("found"))
                          and text_matches(content.get("text"), expected))
            elif r["step"] == "risk":
                ok = (ok and served
                      and (r.get("content") or {}).get("risk") == truth.get("risk"))
            elif r["step"] == "classify":
                if truth.get("type") is not None:
                    ok = (ok and served
                          and (r.get("content") or {}).get("type") == truth.get("type"))
        correct += int(bool(ok) and bool(recs))
    return correct


def classifier_scores(records: list[dict]) -> dict:
    match = total = 0
    confusion: dict[str, int] = {}
    for r in records:
        predicted = (r.get("headers", {}) or {}).get("x-demo-classification")
        if not predicted:
            continue
        total += 1
        truth = r["true_complexity"]
        match += int(predicted.upper() == truth)
        confusion[f"{truth}->{predicted.upper()}"] = confusion.get(f"{truth}->{predicted.upper()}", 0) + 1
    return {
        "labelled": total,
        "accuracy": round(match / total, 3) if total else None,
        "confusion": confusion,
    }


def _shared_backend(pool: str, pools: dict | None) -> str:
    """Group routes that point at the same physical backend.

    A pool map may declare ``shared_with`` explicitly. Older run summaries only
    carry the deploy-time string ``"<deployment> (shared with <route>)"``, so
    parse that as a fallback. Without either, a route is its own backend.
    """
    info = (pools or {}).get(pool) or {}
    explicit = info.get("shared_with")
    if explicit:
        return str(explicit)
    deployment = str(info.get("deployment") or "")
    marker = "(shared with "
    if marker in deployment:
        return deployment.split(marker, 1)[1].split(")", 1)[0].strip()
    return pool


def gpu_device_seconds(metrics: dict | None, pools: dict | None = None) -> float:
    """Measured GPU device time, counting a shared physical backend once."""
    groups: dict[str, float] = {}
    for pool, summary in (metrics or {}).items():
        if pool not in GPU_ROUTES or not isinstance(summary, dict):
            continue
        active = summary.get("device_active_s")
        if active is None:
            continue
        backend = _shared_backend(pool, pools)
        groups[backend] = max(groups.get(backend, 0.0), float(active))
    return sum(groups.values())


def routing_scores(records: list[dict], docs_in_run: int, cores_per_cpu_replica: int,
                   metrics: dict | None = None, pools: dict | None = None) -> dict:
    by_route: dict[str, int] = {}
    gpu_busy_ms = 0.0
    cpu_busy_ms = 0.0
    sensitive_steps = 0
    sensitive_violations = 0
    sensitive_ok = 0
    judgment_steps = 0
    judgment_on_gpu = 0
    termination_routes: dict[str, int] = {}
    fallback = 0
    errors = 0
    for r in records:
        route = (r.get("headers", {}) or {}).get("x-demo-route")
        if route:
            by_route[route] = by_route.get(route, 0) + 1
            stage = (r.get("step") or "").split(":", 1)[0]
            if stage in JUDGMENT_STEPS and "judgment" in (r.get("entry") or ""):
                judgment_steps += 1
                if route in GPU_ROUTES:
                    judgment_on_gpu += 1
            if r.get("step") == "extract:termination":
                termination_routes[route] = termination_routes.get(route, 0) + 1
        busy = (r.get("headers", {}) or {}).get("x-busy-ms")
        try:
            busy = float(busy) if busy else None
        except ValueError:
            busy = None
        if busy is not None:
            if route in GPU_ROUTES:
                gpu_busy_ms += busy
            elif route in CPU_ROUTES:
                cpu_busy_ms += busy
        if r.get("true_sensitive"):
            sensitive_steps += 1
            if route in APPROVED_ROUTES:
                sensitive_ok += 1
            elif route:
                sensitive_violations += 1
        if r.get("outcome") == "fallback" or (r.get("headers", {}) or {}).get("x-praxis-fallback") == "true":
            fallback += 1
        if r.get("outcome", "").startswith(("http_", "transport")):
            errors += 1
    served = sum(by_route.values()) or 1
    docs = max(docs_in_run, 1)

    # Measured device-time from the metrics sampler wins over the simulated
    # pools' response headers; the report labels which source was used. A
    # direct run against real vLLM has neither, and must say so rather than
    # inherit the fake pools' label.
    cost_method = "unmeasured (no device gauges or busy headers in this run)"
    gpu_seconds = gpu_busy_ms / 1000
    cpu_core_seconds = cpu_busy_ms * cores_per_cpu_replica / 1000
    gpu_time_measured = bool(gpu_busy_ms)
    cpu_time_measured = bool(cpu_busy_ms)
    if metrics:
        gpu_groups: dict[str, float] = {}
        cpu_s = 0.0
        for pool, summary in metrics.items():
            if not isinstance(summary, dict):
                continue
            active = (summary or {}).get("device_active_s")
            if active is None:
                continue
            if pool in GPU_ROUTES:
                # Two routes can front the same pod (gpumoe-vllm-approved
                # selects gpumoe-vllm on the ROSA stack). Summing both
                # double-counts one physical GPU; max each backend group.
                backend = _shared_backend(pool, pools)
                gpu_groups[backend] = max(gpu_groups.get(backend, 0.0), float(active))
                gpu_time_measured = True
            elif pool in CPU_ROUTES:
                cpu_s += active
                cpu_time_measured = True
        gpu_s = sum(gpu_groups.values())
        if gpu_s or cpu_s:
            cost_method = "measured (vLLM /metrics samples; per-pool method in metrics.json)"
            gpu_seconds = gpu_s
            cpu_core_seconds = cpu_s * cores_per_cpu_replica if cpu_s else cpu_s
    elif gpu_busy_ms or cpu_busy_ms:
        cost_method = "simulated (X-Busy-Ms headers from the fake pools)"

    return {
        "routes": by_route,
        "cpu_served_share": round(by_route.get("vllm-cpu", 0) / served, 3),
        "needs_cpu": by_route.get("vllm-cpu", 0) > 0,
        "needs_gpu": any(by_route.get(p, 0) for p in GPU_ROUTES),
        "gpu_seconds": round(gpu_seconds, 1),
        "cpu_core_seconds": round(cpu_core_seconds, 1),
        "gpu_seconds_per_doc": round(gpu_seconds / docs, 3),
        "cpu_core_seconds_per_doc": round(cpu_core_seconds / docs, 3),
        "gpu_time_measured": gpu_time_measured,
        "cpu_time_measured": cpu_time_measured,
        "device_time_measured": gpu_seconds or cpu_core_seconds,
        "cost_method": cost_method,
        "sensitive_steps": sensitive_steps,
        "sensitive_on_approved": sensitive_ok,
        "sensitive_violations": sensitive_violations,
        "judgment_steps": judgment_steps,
        "judgment_on_gpu": judgment_on_gpu,
        "termination_routes": termination_routes,
        "fallback": fallback,
        "errors": errors,
    }


def sensitivity_breakdown(records: list[dict], truth_docs: dict[str, dict]) -> dict:
    """Quality and routes split by the dataset's own sensitivity label.

    The label is the dataset's truth, never the pipeline's screen: the screen
    decides routing, so scoring against its own decision would be circular.
    CUAD ships no sensitivity truth (``sensitive: None``); those documents are
    counted as unlabelled and must not be folded into "non-sensitive".
    """
    groups: dict[str, dict] = {
        "sensitive": {"docs": set(), "records": []},
        "non-sensitive": {"docs": set(), "records": []},
    }
    unlabelled: set[str] = set()
    for r in records:
        doc_id = r.get("doc_id")
        if not doc_id:
            continue
        label = (truth_docs.get(doc_id) or {}).get("sensitive")
        if label is None:
            unlabelled.add(doc_id)
            continue
        key = "sensitive" if label else "non-sensitive"
        groups[key]["docs"].add(doc_id)
        groups[key]["records"].append(r)

    out: dict[str, dict | None] = {}
    for key, group in groups.items():
        recs = group["records"]
        if not recs:
            out[key] = None
            continue
        routes: dict[str, int] = {}
        for r in recs:
            route = (r.get("headers", {}) or {}).get("x-demo-route")
            if route:
                routes[route] = routes.get(route, 0) + 1
        out[key] = {
            "docs": len(group["docs"]),
            "steps": len(recs),
            "extraction": extraction_scores(recs, truth_docs),
            "risk_accuracy": step_accuracy(recs, truth_docs, "risk", "risk"),
            "type_accuracy": step_accuracy(recs, truth_docs, "classify", "type"),
            "docs_fully_correct": docs_fully_correct(recs, truth_docs),
            "routes": routes,
        }
    return {"groups": out, "unlabelled_docs": len(unlabelled)}


def score_run(run: dict, truth_docs: dict[str, dict], cores_per_cpu_replica: int,
              doc_texts: dict[str, str] | None = None) -> dict:
    records = run["records"]
    summary = run["summary"]
    run_doc_ids = {r["doc_id"] for r in records if r["doc_id"]}
    docs_in_run = len(run_doc_ids)
    metrics = None
    metrics_path = Path(run["dir"]) / "metrics.json"
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text())
        # The sampler summary is a dict of pool -> summary. A direct run writes
        # its *quality* scores to the same filename (floats and one dict), so
        # never hand that to the routing scorer: it is not device-time.
        if not isinstance(metrics, dict) or not all(
                isinstance(v, dict) for v in metrics.values()):
            metrics = None
    arm = summary.get("arm")
    if summary.get("mode") == "direct":
        # Every direct run calls itself "direct". Use the pool directory
        # (cpu-moe / gpu-moe / cpu-small) so three pools can share one report
        # without three identical rows.
        arm = Path(run["dir"]).name
    # Display name from the canonical map; old runs only carry the letter.
    # Direct runs are not routing configs, so they keep their pool directory.
    config = summary.get("config") or name_for_key(summary.get("arm"))
    if config is None and summary.get("mode") == "direct":
        config = f"direct/{arm}"
    return {
        "arm": arm,
        "config": config or arm,
        "mode": summary.get("mode"),
        "docs": summary.get("docs"),
        "wall_s": summary.get("wall_s"),
        "run_doc_ids": sorted(run_doc_ids),
        "extraction": extraction_scores(records, truth_docs, doc_texts),
        "risk_accuracy": step_accuracy(records, truth_docs, "risk", "risk"),
        "type_accuracy": step_accuracy(records, truth_docs, "classify", "type"),
        "report_accuracy": report_score(records, truth_docs),
        "docs_fully_correct": docs_fully_correct(records, truth_docs),
        "classifier": classifier_scores(records),
        "screen": summary.get("screen"),
        "sensitivity": sensitivity_breakdown(records, truth_docs),
        # The pools map is recorded with the run; the scorer uses it to avoid
        # counting one physical backend twice when two routes front it.
        "pools": summary.get("pools") or {},
        "routing": routing_scores(records, docs_in_run, cores_per_cpu_replica, metrics,
                                  pools=summary.get("pools") or {}),
    }


def dataset_description(manifest: dict) -> str:
    """Provenance from the manifest actually scored, never hardcoded."""
    source = str(manifest.get("source") or "")
    count = manifest.get("count", len(manifest.get("documents", [])))
    seed = manifest.get("seed")
    if "cuad" in source.lower():
        return (f"{count} CUAD v1 real contracts ({source}; "
                f"seed {seed}; licences: CC BY 4.0 - verify before redistribution)")
    return (f"{count} synthetic documents (seed {seed}, licences: synthetic, "
            f"no personal data)")


def build_report(run_dirs: list[str], data_dir: str, cores_per_cpu_replica: int,
                 rates: dict | None = None) -> tuple[str, dict]:
    manifest = json.loads((Path(data_dir) / "ground_truth.json").read_text())
    truth_docs = {d["doc_id"]: d for d in manifest["documents"]}
    doc_texts: dict[str, str] = {}
    documents_dir = Path(data_dir) / "documents"
    if documents_dir.is_dir():
        for path in documents_dir.glob("*.txt"):
            doc_texts[path.stem] = path.read_text(errors="replace")
    scored = [score_run(load_run(d), truth_docs, cores_per_cpu_replica, doc_texts)
              for d in run_dirs]
    dataset = dataset_description(manifest)

    lines: list[str] = []
    lines.append("# Hybrid CPU+GPU demo - measurement report")
    lines.append("")
    lines.append(f"Ground truth: {dataset}.")
    lines.append("Each config's table row names the number of documents it actually ran.")
    lines.append("Device-time is measured from the real pools' `/metrics` samples where the "
                 "sampler succeeded; simulated `X-Busy-Ms` headers are labeled as such, and a "
                 "run with neither prints `n/a`, never zero. Dollar figures are illustrative "
                 "sweeps over the measured device time, not a quote.")
    lines.append("")

    lines.append("## Configs overview")
    lines.append("")
    lines.append("| config | mode | docs | wall s | errors | fallback (degraded) |")
    lines.append("|---|---|---|---|---|---|")
    for s in scored:
        lines.append(f"| {s['config']} | {s['mode']} | {s['docs']} | {s['wall_s']} | "
                     f"{s['routing']['errors']} | {s['routing']['fallback']} |")
    lines.append("")

    lines.append("## Quality vs ground truth")
    lines.append("")
    lines.append(f"| config | extract P ({MATCH_METRIC}) | extract R ({MATCH_METRIC}) | extract F1 | "
                 "risk acc | type acc | report acc | docs fully correct |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in scored:
        e = s["extraction"]
        lines.append(f"| {s['config']} | {e['precision']} | {e['recall']} | {e['f1']} | "
                     f"{s['risk_accuracy']} | {s['type_accuracy']} | {s['report_accuracy']} | "
                     f"{s['docs_fully_correct']}/{s['docs']} |")
    baseline = majority_risk_baseline(truth_docs, {d for s in scored for d in s.get("run_doc_ids", [])})
    lines.append(f"| *majority-class baseline* | — | — | — | *{baseline}* | — | — | — |")
    lines.append("")
    lines.append("How to read the quality numbers:")
    lines.append("")
    lines.append("- **Extraction** is a copy task: quote the clause. Small models do it as well as")
    lines.append("  (here: better than) large ones, so the CPU pool takes the high-volume steps")
    lines.append(f"  without a quality cost. Scoring is {MATCH_METRIC} (normalised substring in")
    lines.append("  either direction), not span-exact: a wider quote that contains the annotated")
    lines.append("  clause passes, and a correct paraphrase fails.")
    lines.append("  The JSON alongside this report also carries `exact` (string equality) and")
    lines.append("  `partial` (token Jaccard) views, plus nervaluate's strict/exact/partial/type")
    lines.append("  offset strategies when document text and nervaluate are available. The table")
    lines.append("  keeps containment as the primary number so historical runs stay comparable.")
    lines.append("- **Risk accuracy** is rubric-following judgment. Compare it against the majority")
    lines.append(f"  baseline ({baseline} on this document set): a model at the baseline is not")
    lines.append("  applying the rubric at all. A model below it is worse than always guessing")
    lines.append("  'low'. This is where the CPU/GPU quality trade-off actually lives, and it is")
    lines.append("  the number the agreed tolerance (V8) should be set against. Note the rubric is")
    lines.append("  deterministic given the extractions - a rule in the pipeline would score ~1.0;")
    lines.append("  the LLM step measures model judgment, not what the pipeline could hard-code.")
    lines.append("- **Report accuracy** aggregates the per-document results, so it inherits every")
    lines.append("  upstream error (propagation), plus its own synthesis errors.")
    lines.append("")

    lines.append("## Sensitive vs non-sensitive documents")
    lines.append("")
    labelled = any(
        (s["sensitivity"]["groups"]["sensitive"] or s["sensitivity"]["groups"]["non-sensitive"])
        for s in scored)
    if labelled:
        lines.append("| config | class | docs | extract F1 | risk acc | type acc | docs fully correct | routes |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for s in scored:
            for key in ("non-sensitive", "sensitive"):
                g = s["sensitivity"]["groups"][key]
                if not g:
                    continue
                routes = ", ".join(f"{k}:{v}" for k, v in sorted(g["routes"].items())) or "—"
                lines.append(f"| {s['config']} | {key} | {g['docs']} | {g['extraction']['f1']} | "
                             f"{g['risk_accuracy']} | {g['type_accuracy']} | "
                             f"{g['docs_fully_correct']}/{g['docs']} | {routes} |")
        lines.append("")
        lines.append("Class comes from the dataset's own `sensitive` label, never from the")
        lines.append("pipeline's screen: the screen decides routing, so scoring against its own")
        lines.append("decision would be circular. Enforcement (sensitive steps on non-approved")
        lines.append("pools) is still counted from the screen's decisions in the next section.")
        unlabelled = max((s["sensitivity"]["unlabelled_docs"] for s in scored), default=0)
        if unlabelled:
            lines.append("")
            lines.append(f"{unlabelled} document(s) in these runs carry no sensitivity label "
                         "and are excluded from this table.")
        lines.append("")
    else:
        lines.append("This dataset carries no sensitivity ground truth (CUAD ships")
        lines.append("`sensitive: None`), so a sensitive/non-sensitive quality split is not")
        lines.append("measurable here; only the enforcement counts in the next section apply.")
        lines.append("")

    lines.append("## Classifier and sensitivity screen")
    lines.append("")
    lines.append("| config | labelled | complexity accuracy | screen tp/fp/tn/fn | screen precision | screen recall |")
    lines.append("|---|---|---|---|---|---|")
    for s in scored:
        screen = s["screen"] or {}
        tp, fp = screen.get("tp", 0), screen.get("fp", 0)
        fn, tn = screen.get("fn", 0), screen.get("tn", 0)
        precision = round(tp / (tp + fp), 3) if tp + fp else None
        recall = round(tp / (tp + fn), 3) if tp + fn else None
        lines.append(f"| {s['config']} | {s['classifier']['labelled']} | {s['classifier']['accuracy']} | "
                     f"{tp}/{fp}/{tn}/{fn} | {precision} | {recall} |")
    lines.append("")
    # Do not let a high CPU-served share imply good routing: when the
    # classifier labels nearly every step MEDIUM, CPU-first routing produces
    # that share without separating the classes at all.
    weak = [s for s in scored
            if s["classifier"].get("labelled")
            and (s["classifier"]["accuracy"] or 0) < 0.6]
    if weak:
        lines.append("**Classifier caveat:** " + ", ".join(
            f"config {s['config']} agreement {s['classifier']['accuracy']}" for s in weak)
            + ". This column compares the classifier's prompt-difficulty label with the "
              "dataset's per-step label: a low value means the two taxonomies differ, "
              "not that the classifier failed - read the confusion matrix. At this value "
              "a high CPU-served share reflects the CPU-first policy, not demonstrated "
              "four-way complexity separation.")
        lines.append("")
    confusion = scored[-1]["classifier"]["confusion"] if scored else {}
    if confusion:
        lines.append("Label confusion (truth->predicted, hybrid config): "
                     + ", ".join(f"{k}={v}" for k, v in sorted(confusion.items())))
        lines.append("")

    lines.append("## Routing and governance")
    lines.append("")
    lines.append("| config | CPU-served share | sensitive steps | on approved pool | violations |")
    lines.append("|---|---|---|---|---|")
    unrouted: list[str] = []
    for s in scored:
        r = s["routing"]
        if not r["routes"]:
            unrouted.append(s["config"])
            lines.append(f"| {s['config']} | n/a | n/a | n/a | n/a |")
            continue
        lines.append(f"| {s['config']} | {r['cpu_served_share']} | {r['sensitive_steps']} | "
                     f"{r['sensitive_on_approved']} | {r['sensitive_violations']} |")
    lines.append("")
    lines.append("Violations count sensitive-document steps served by a pool outside the approved "
                 "set (vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.")
    judgment = [s for s in scored if s["routing"].get("judgment_steps")]
    if judgment:
        lines.append("")
        lines.append("Judgment split (risk + report through dedicated entries): "
                     + ", ".join(f"config {s['config']} {s['routing']['judgment_on_gpu']}/"
                                 f"{s['routing']['judgment_steps']} on GPU" for s in judgment)
                     + ".")
    termination = [s for s in scored if s["routing"].get("termination_routes")]
    if termination:
        lines.append("")
        lines.append("Termination extraction routes (CPU specialist; the canonical CUAD "
                     "convenience question is asked first and a cause follow-up only when it "
                     "finds nothing and the contract has cause language): "
                     + "; ".join(f"config {s['config']} "
                                 + ", ".join(f"{route}={n}" for route, n
                                             in sorted(s["routing"]["termination_routes"].items()))
                                 for s in termination)
                     + ".")
    if unrouted:
        lines.append("")
        lines.append("Direct run(s) " + ", ".join(unrouted) + " carry no route headers: one bare "
                     "endpoint receives every request, so routing and governance are not scored. "
                     "`n/a` is not zero - by construction each direct run served all of its "
                     "sensitive documents on that single pool.")
    lines.append("")

    lines.append("## Cost (device-time, then priced as a sweep - no single $ figure)")
    lines.append("")
    by_method: dict[str, list[str]] = {}
    for s in scored:
        by_method.setdefault(s["routing"].get("cost_method", "simulated"), []).append(s["config"])
    for method, arms in sorted(by_method.items()):
        lines.append(f"- device-time source: {method.split(' (')[0]} ({', '.join(arms)})")
    lines.append("")
    lines.append("| config | GPU-s/doc | CPU core-s/doc |")
    lines.append("|---|---|---|")
    for s in scored:
        r = s["routing"]
        if not r["routes"]:
            lines.append(f"| {s['config']} | n/a | n/a |")
            continue
        if not r.get("needs_gpu"):
            gpu = "—"
        else:
            gpu = r["gpu_seconds_per_doc"] if r.get("gpu_time_measured", True) else "n/a"
        if not r.get("needs_cpu"):
            cpu = "—"
        else:
            cpu = r["cpu_core_seconds_per_doc"] if r.get("cpu_time_measured", True) else "n/a"
        lines.append(f"| {s['config']} | {gpu} | {cpu} |")
    lines.append("")
    lines.append("- GPU-s/doc counts each physical backend once. Routes whose pool map "
                 "declares `shared_with` (or a deployment marked `(shared with ...)`) are "
                 "maxed, not summed.")
    route_hardware: set[str] = set()
    for s in scored:
        for route, info in (s.get("pools") or {}).items():
            if route not in GPU_ROUTES:
                continue
            hardware = str((info or {}).get("hardware") or "").lower()
            if hardware.startswith("cpu") or "cpu (" in hardware:
                route_hardware.add(route)
    for route in sorted(route_hardware):
        lines.append(f"- Route `{route}` is accounted as GPU but its pool map records CPU "
                     "hardware; its GPU-s/doc is route-name accounting, not GPU device time.")
    lines.append("")
    unmeasured = {s["config"] for s in scored
                  if (s["routing"].get("needs_cpu")
                      and not s["routing"].get("cpu_time_measured", True))
                  or (s["routing"].get("needs_gpu")
                      and not s["routing"].get("gpu_time_measured", True))}
    if unmeasured:
        lines.append(f"Config(s) {', '.join(sorted(unmeasured))}: device time is **not measured** "
                     "for a pool that served steps (the relay front-end does not expose vLLM's "
                     "in-flight gauges), so no dollar figure is quoted; `n/a` is not zero.")
        lines.append("")
    if any(not s["routing"]["routes"] for s in scored):
        lines.append("Direct runs have no route headers, so their GPU/CPU split is not "
                     "attributable either; `n/a` is not zero.")
        lines.append("")
    priced = [s for s in scored
              if s["config"] not in unmeasured and s["routing"]["device_time_measured"]]
    if priced:
        lines.append("Dollar sweep (ILLUSTRATIVE rates; replace with the agreed cost model before "
                     "any number leaves this repo):")
        lines.append("")
        for s in priced:
            r = s["routing"]
            lines.append(f"- config {s['config']}:")
            for row in cost_model.sweep(r["gpu_seconds"], r["cpu_core_seconds"], s["docs"], rates):
                lines.append(f"  - {row['scenario']}: ${row['dollar_per_doc']}/document "
                             f"(gpu ${row['gpu_hour']}/h, core ${row['core_hour']}/h)")
        lines.append("")
    else:
        lines.append("No config has both GPU and CPU device time measured; no dollar figure is "
                     "quoted until the unmeasured pool is backfilled.")
        lines.append("")

    lines.append("## Resilience")
    lines.append("")
    lines.append("| config | fallback (degraded) | errors |")
    lines.append("|---|---|---|")
    for s in scored:
        lines.append(f"| {s['config']} | {s['routing']['fallback']} | {s['routing']['errors']} |")
    lines.append("")

    report_json = {"truth_docs": len(truth_docs), "dataset": dataset, "arms": scored}
    return "\n".join(lines), report_json


def governance_check(trace_ui_base: str, run_dir: str | None = None) -> dict:
    """Cross-check the gateway's own trace records against the governance claim.

    Reads /_trace/api/summary from the trace UI and verifies that every
    sensitive-entry trace landed on an approved pool. Optionally compares the
    aggregate counts with the pipeline's own records for the given run.
    """
    resp = httpx.get(trace_ui_base.rstrip("/") + "/_trace/api/summary", timeout=30.0)
    resp.raise_for_status()
    summary = resp.json()
    violations = []
    sensitive_total = 0
    for group in summary.get("groups", []):
        # Both the stage-agnostic sensitive entry and the sensitive-judgment
        # entry carry PII; a check that only matched "sensitive" would miss
        # risk/report traces and understate the audit.
        if not (group.get("entry") or "").startswith("sensitive"):
            continue
        sensitive_total += group["count"]
        # A failed step with no route is not evidence of a leak; only a
        # recorded non-approved destination is.
        if group.get("route") and group["route"] not in APPROVED_ROUTES:
            violations.append(group)
    result = {
        "trace_total": summary.get("total"),
        "sensitive_entry_traces": sensitive_total,
        "sensitive_entry_violations": violations,
        "routes": summary.get("routes"),
        "clean": not violations,
    }
    if run_dir:
        run = load_run(run_dir)
        mine: dict[str, int] = {}
        pipeline_sensitive = 0
        for r in run["records"]:
            route = (r.get("headers", {}) or {}).get("x-demo-route")
            if route:
                mine[route] = mine.get(route, 0) + 1
            if r.get("true_sensitive") and route:
                pipeline_sensitive += 1
        result["pipeline_routes"] = mine
        result["pipeline_sensitive_steps"] = pipeline_sensitive
        result["trace_sensitive_steps"] = sensitive_total
        # Raw traces are a rolling window (MAX_TRACES). If the run produced
        # more sensitive steps than the window retained, "clean" is only a
        # statement about the retained tail, not the whole run.
        complete = sensitive_total >= pipeline_sensitive
        result["trace_window_complete"] = complete
        if not complete:
            result["clean"] = False
            result["reason"] = (
                f"trace window retained {sensitive_total} of "
                f"{pipeline_sensitive} sensitive steps; re-run with "
                "TRACE_MAX_TRACES >= the run's step count before claiming zero"
            )
    return result


HTML_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Hybrid demo report</title>
<style>
:root{color-scheme:dark}body{margin:0;padding:32px;background:#07111f;color:#e9f2ff;
font:15px/1.5 Inter,ui-sans-serif,system-ui,sans-serif}
main{max-width:1000px;margin:auto}h1{font-size:24px}h2{font-size:18px;margin-top:32px}
table{border-collapse:collapse;width:100%;margin:12px 0}
th,td{border:1px solid #2b4865;padding:6px 10px;text-align:left}
th{background:#0d1b2d}tr:nth-child(even) td{background:#0b1626}
.bad{color:#ff4d5e;font-weight:700}.good{color:#55e6a0}
p.note{color:#9db1c8}
</style></head><body><main>
<h1>Hybrid CPU+GPU demo - measurement report</h1>
<p class="note">__NOTE__</p>
__BODY__
</main></body></html>
"""


def render_html(report_json: dict) -> str:
    parts = []
    for title, headers, rows in _html_tables(report_json):
        parts.append(f"<h2>{title}</h2><table><thead><tr>"
                     + "".join(f"<th>{h}</th>" for h in headers)
                     + "</tr></thead><tbody>"
                     + "".join("<tr>" + "".join(f"<td{r[1][i]}>{r[0][i]}</td>" for i in range(len(headers)))
                               + "</tr>" for r in rows)
                     + "</tbody></table>")
    dataset = report_json.get("dataset") or f"{report_json.get('truth_docs')} documents"
    note = ("Device-time and dollar figures are SIMULATED where the fake pool backends were used; "
            f"ground truth: {dataset}.")
    weak = [s for s in report_json.get("arms", [])
            if s.get("classifier", {}).get("labelled")
            and (s["classifier"]["accuracy"] or 0) < 0.6]
    if weak:
        note += (" Classifier caveat: agreement "
                 + ", ".join(f"{s['config']}={s['classifier']['accuracy']}" for s in weak)
                 + " (taxonomy mismatch, not a failure - read the confusion matrix); "
                   "the CPU-served share is not evidence of four-way complexity routing.")
    return HTML_PAGE.replace("__NOTE__", note).replace("__BODY__", "\n".join(parts))


def _html_tables(report_json: dict) -> list[tuple[str, list[str], list[tuple[list, list]]]]:
    arms = report_json.get("arms", [])
    tables = []

    headers = ["config", "mode", "docs", "wall s", "errors", "fallback (degraded)"]
    rows = []
    for s in arms:
        rows.append(([s["config"], s["mode"], s["docs"], s["wall_s"],
                      s["routing"]["errors"], s["routing"]["fallback"]],
                     ["", "", "", "", "", ""]))
    tables.append(("Configs overview", headers, rows))

    headers = ["config", f"extract P ({MATCH_METRIC})", f"extract R ({MATCH_METRIC})",
               "extract F1", "risk acc", "type acc", "report acc", "docs fully correct"]
    rows = []
    for s in arms:
        e = s["extraction"]
        rows.append(([s["config"], e["precision"], e["recall"], e["f1"], s["risk_accuracy"],
                      s["type_accuracy"], s["report_accuracy"],
                      f"{s['docs_fully_correct']}/{s['docs']}"], [""] * 8))
    tables.append(("Quality vs ground truth", headers, rows))

    headers = ["config", "CPU-served share", "sensitive steps", "on approved pool", "violations"]
    rows = []
    for s in arms:
        r = s["routing"]
        rows.append(([s["config"], r["cpu_served_share"], r["sensitive_steps"],
                      r["sensitive_on_approved"], r["sensitive_violations"]],
                     ["", "", "", "", ' class="bad"' if r["sensitive_violations"] else ' class="good"']))
    tables.append(("Routing and governance", headers, rows))

    headers = ["config", "GPU-s/doc", "CPU core-s/doc"]
    rows = []
    for s in arms:
        r = s["routing"]
        if not r.get("needs_gpu"):
            gpu = "—"
        else:
            gpu = r["gpu_seconds_per_doc"] if r.get("gpu_time_measured", True) else "n/a"
        if not r.get("needs_cpu"):
            cpu = "—"
        else:
            cpu = r["cpu_core_seconds_per_doc"] if r.get("cpu_time_measured", True) else "n/a"
        rows.append(([s["config"], gpu, cpu], ["", "", ""]))
    methods = {s["routing"].get("cost_method", "") for s in arms}
    source = "measured" if methods and all(m.startswith("measured") for m in methods) else "see device-time source"
    tables.append((f"Cost (device-time; {source})", headers, rows))
    return tables


def main() -> None:
    parser = argparse.ArgumentParser(description="score hybrid demo runs")
    sub = parser.add_subparsers(dest="command")

    report = sub.add_parser("report", help="build the measurement report from run dirs")
    # action="append" so the documented `--runs A --runs B ...` form works;
    # a single `--runs A B ...` keeps working too.
    report.add_argument("--runs", nargs="+", action="append", required=True)
    report.add_argument("--data", default="data/synthetic")
    report.add_argument("--out", required=True, help="output path for report.md (json alongside)")
    report.add_argument("--cores-per-cpu-replica", type=int, default=8)
    report.add_argument("--rates-json", default=None, help="cost rates JSON overriding defaults")

    gov = sub.add_parser("governance", help="verify sensitive-entry traces stayed on approved pools")
    gov.add_argument("--trace-ui", required=True)
    gov.add_argument("--run", default=None)

    args = parser.parse_args()
    if args.command == "governance":
        print(json.dumps(governance_check(args.trace_ui, args.run), indent=1))
        return
    rates = json.loads(Path(args.rates_json).read_text()) if args.rates_json else None
    runs = [run_dir for group in args.runs for run_dir in group]
    text, data = build_report(runs, args.data, args.cores_per_cpu_replica, rates)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    out.with_suffix(".json").write_text(json.dumps(data, indent=1))
    out.with_suffix(".html").write_text(render_html(data))
    print(text)


if __name__ == "__main__":
    main()
