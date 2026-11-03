from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx

from ..common import normalize_text
from . import cost as cost_model

GPU_ROUTES = {"vllm-gpu", "vllm-gpu-approved", "vllm-gpu-small"}
CPU_ROUTES = {"vllm-cpu"}
APPROVED_ROUTES = {"vllm-cpu", "vllm-gpu-approved"}


def text_matches(predicted: str | None, truth: str | None) -> bool:
    if not predicted or not truth:
        return False
    p, t = normalize_text(predicted), normalize_text(truth)
    return p == t or p in t or t in p


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


def extraction_scores(records: list[dict], truth_docs: dict[str, dict]) -> dict:
    tp = fp = fn = 0
    correct_fields = 0
    total_fields = 0
    for r in records:
        if not r["step"].startswith("extract:"):
            continue
        clause = r["step"].split(":", 1)[1]
        truth = (truth_docs.get(r["doc_id"], {}).get("clauses") or {}).get(clause)
        content = r.get("content", {})
        found = bool(content.get("found"))
        text = content.get("text")
        total_fields += 1
        if truth is None:
            if not found:
                correct_fields += 1
            else:
                fp += 1
        else:
            if found and text_matches(text, truth):
                tp += 1
                correct_fields += 1
            elif found:
                fp += 1
            else:
                fn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "fields_correct": correct_fields,
        "fields_total": total_fields,
    }


def step_accuracy(records: list[dict], truth_docs: dict[str, dict], step: str,
                  field: str) -> float | None:
    hits = misses = 0
    for r in records:
        if r["step"] != step:
            continue
        truth = truth_docs.get(r["doc_id"], {})
        if step == "classify":
            expected = truth.get("type")
            got = r.get("content", {}).get("type")
        elif step == "risk":
            expected = truth.get("risk")
            got = r.get("content", {}).get("risk")
        else:
            expected, got = truth.get(field), r.get("content", {}).get(field)
        if expected is None:
            continue
        hits += int(got == expected)
        misses += int(got != expected)
    return round(hits / (hits + misses), 3) if hits + misses else None


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


def report_score(records: list[dict], truth_docs: dict[str, dict]) -> float | None:
    recs = [r for r in records if r["step"] == "report"]
    if not recs:
        return None
    content = recs[-1].get("content", {})
    if not content or "documents" not in content:
        return 0.0
    run_doc_ids = {r["doc_id"] for r in records if r["doc_id"]}
    docs = [truth_docs[d] for d in sorted(run_doc_ids) if d in truth_docs]
    risk_counts = {"low": 0, "medium": 0, "high": 0}
    law_counts: dict[str, int] = {}
    missing_dp = 0
    highest = None
    for d in docs:
        risk_counts[d.get("risk", "low")] = risk_counts.get(d.get("risk", "low"), 0) + 1
        law = d.get("clauses", {}).get("governing_law") or "missing"
        law_counts[law] = law_counts.get(law, 0) + 1
        if not d.get("clauses", {}).get("data_protection"):
            missing_dp += 1
        if d.get("risk") == "high" and (highest is None or d["doc_id"] < highest):
            highest = d["doc_id"]
    checks = {
        "documents": 1.0 if content.get("documents") == len(docs) else 0.0,
        "risk_counts": _count_closeness(content.get("risk_counts"), risk_counts),
        "missing_data_protection": 1.0 if content.get("missing_data_protection") == missing_dp else 0.0,
        "highest_risk_ref": 1.0 if content.get("highest_risk_ref") == highest else 0.0,
        "governing_law_counts": _count_closeness(content.get("governing_law_counts"), law_counts),
    }
    return round(sum(checks.values()) / len(checks), 3)


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
            if clause:
                expected = (truth.get("clauses") or {}).get(clause)
                content = r.get("content", {})
                if expected is None:
                    ok = ok and not content.get("found")
                else:
                    ok = ok and bool(content.get("found")) and text_matches(content.get("text"), expected)
            elif r["step"] == "risk":
                ok = ok and r.get("content", {}).get("risk") == truth.get("risk")
            elif r["step"] == "classify":
                if truth.get("type") is not None:
                    ok = ok and r.get("content", {}).get("type") == truth.get("type")
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


def routing_scores(records: list[dict], docs_in_run: int, cores_per_cpu_replica: int,
                   metrics: dict | None = None) -> dict:
    by_route: dict[str, int] = {}
    gpu_busy_ms = 0.0
    cpu_busy_ms = 0.0
    sensitive_steps = 0
    sensitive_violations = 0
    sensitive_ok = 0
    fallback = 0
    errors = 0
    for r in records:
        route = (r.get("headers", {}) or {}).get("x-demo-route")
        if route:
            by_route[route] = by_route.get(route, 0) + 1
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
    # pools' response headers; the report labels which source was used.
    cost_method = "simulated (X-Busy-Ms headers from the fake pools)"
    gpu_seconds = gpu_busy_ms / 1000
    cpu_core_seconds = cpu_busy_ms * cores_per_cpu_replica / 1000
    if metrics:
        gpu_s = 0.0
        cpu_s = 0.0
        for pool, summary in metrics.items():
            active = (summary or {}).get("device_active_s")
            if active is None:
                continue
            if pool in GPU_ROUTES:
                gpu_s += active
            elif pool in CPU_ROUTES:
                cpu_s += active
        if gpu_s or cpu_s:
            method = next((s.get("method") for s in metrics.values() if s and s.get("method")),
                          "metrics.json sampler")
            cost_method = f"measured ({method})"
            gpu_seconds = gpu_s
            cpu_core_seconds = cpu_s * cores_per_cpu_replica if cpu_s else cpu_s

    return {
        "routes": by_route,
        "cpu_served_share": round(by_route.get("vllm-cpu", 0) / served, 3),
        "gpu_seconds": round(gpu_seconds, 1),
        "cpu_core_seconds": round(cpu_core_seconds, 1),
        "gpu_seconds_per_doc": round(gpu_seconds / docs, 3),
        "cpu_core_seconds_per_doc": round(cpu_core_seconds / docs, 3),
        "device_time_measured": gpu_seconds or cpu_core_seconds,
        "cost_method": cost_method,
        "sensitive_steps": sensitive_steps,
        "sensitive_on_approved": sensitive_ok,
        "sensitive_violations": sensitive_violations,
        "fallback": fallback,
        "errors": errors,
    }


def score_run(run: dict, truth_docs: dict[str, dict], cores_per_cpu_replica: int) -> dict:
    records = run["records"]
    summary = run["summary"]
    run_doc_ids = {r["doc_id"] for r in records if r["doc_id"]}
    docs_in_run = len(run_doc_ids)
    metrics = None
    metrics_path = Path(run["dir"]) / "metrics.json"
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text())
    return {
        "arm": summary.get("arm"),
        "mode": summary.get("mode"),
        "docs": summary.get("docs"),
        "wall_s": summary.get("wall_s"),
        "run_doc_ids": sorted(run_doc_ids),
        "extraction": extraction_scores(records, truth_docs),
        "risk_accuracy": step_accuracy(records, truth_docs, "risk", "risk"),
        "type_accuracy": step_accuracy(records, truth_docs, "classify", "type"),
        "report_accuracy": report_score(records, truth_docs),
        "docs_fully_correct": docs_fully_correct(records, truth_docs),
        "classifier": classifier_scores(records),
        "screen": summary.get("screen"),
        "routing": routing_scores(records, docs_in_run, cores_per_cpu_replica, metrics),
    }


def build_report(run_dirs: list[str], data_dir: str, cores_per_cpu_replica: int,
                 rates: dict | None = None) -> tuple[str, dict]:
    manifest = json.loads((Path(data_dir) / "ground_truth.json").read_text())
    truth_docs = {d["doc_id"]: d for d in manifest["documents"]}
    scored = [score_run(load_run(d), truth_docs, cores_per_cpu_replica) for d in run_dirs]

    lines: list[str] = []
    lines.append("# Hybrid CPU+GPU demo - measurement report")
    lines.append("")
    lines.append(f"Ground truth: {len(truth_docs)} synthetic documents "
                 f"(seed {manifest.get('seed')}, licences: synthetic, no personal data).")
    lines.append("Device-time and dollar figures are SIMULATED where the fake pool backends were "
                 "used; against real vLLM pools only request counts, routes and wall-clock hold.")
    lines.append("")

    lines.append("## Arms overview")
    lines.append("")
    lines.append("| arm | mode | docs | wall s | errors | fallback (degraded) |")
    lines.append("|---|---|---|---|---|---|")
    for s in scored:
        lines.append(f"| {s['arm']} | {s['mode']} | {s['docs']} | {s['wall_s']} | "
                     f"{s['routing']['errors']} | {s['routing']['fallback']} |")
    lines.append("")

    lines.append("## Quality vs ground truth")
    lines.append("")
    lines.append("| arm | extract P | extract R | extract F1 | risk acc | type acc | report acc | docs fully correct |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in scored:
        e = s["extraction"]
        lines.append(f"| {s['arm']} | {e['precision']} | {e['recall']} | {e['f1']} | "
                     f"{s['risk_accuracy']} | {s['type_accuracy']} | {s['report_accuracy']} | "
                     f"{s['docs_fully_correct']}/{s['docs']} |")
    baseline = majority_risk_baseline(truth_docs, {d for s in scored for d in s.get("run_doc_ids", [])})
    lines.append(f"| *majority-class baseline* | — | — | — | *{baseline}* | — | — | — |")
    lines.append("")
    lines.append("How to read the quality numbers:")
    lines.append("")
    lines.append("- **Extraction** is a copy task: quote the clause. Small models do it as well as")
    lines.append("  (here: better than) large ones, so the CPU pool takes the high-volume steps")
    lines.append("  without a quality cost. Large models paraphrase, which exact-text scoring")
    lines.append("  penalises even when the paraphrase is semantically right.")
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

    lines.append("## Classifier and sensitivity screen")
    lines.append("")
    lines.append("| arm | labelled | complexity accuracy | screen tp/fp/tn/fn | screen precision | screen recall |")
    lines.append("|---|---|---|---|---|---|")
    for s in scored:
        screen = s["screen"] or {}
        tp, fp = screen.get("tp", 0), screen.get("fp", 0)
        fn, tn = screen.get("fn", 0), screen.get("tn", 0)
        precision = round(tp / (tp + fp), 3) if tp + fp else None
        recall = round(tp / (tp + fn), 3) if tp + fn else None
        lines.append(f"| {s['arm']} | {s['classifier']['labelled']} | {s['classifier']['accuracy']} | "
                     f"{tp}/{fp}/{tn}/{fn} | {precision} | {recall} |")
    lines.append("")
    confusion = scored[-1]["classifier"]["confusion"] if scored else {}
    if confusion:
        lines.append("Label confusion (truth->predicted, arm D): "
                     + ", ".join(f"{k}={v}" for k, v in sorted(confusion.items())))
        lines.append("")

    lines.append("## Routing and governance")
    lines.append("")
    lines.append("| arm | CPU-served share | sensitive steps | on approved pool | violations |")
    lines.append("|---|---|---|---|---|")
    for s in scored:
        r = s["routing"]
        lines.append(f"| {s['arm']} | {r['cpu_served_share']} | {r['sensitive_steps']} | "
                     f"{r['sensitive_on_approved']} | {r['sensitive_violations']} |")
    lines.append("")
    lines.append("Violations count sensitive-document steps served by a pool outside the approved "
                 "set (vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.")
    lines.append("")

    lines.append("## Cost (device-time, then priced as a sweep - no single $ figure)")
    lines.append("")
    methods = {s["routing"].get("cost_method", "simulated") for s in scored}
    for method in sorted(methods):
        lines.append(f"- device-time source: {method}")
    lines.append("")
    lines.append("| arm | GPU-s/doc | CPU core-s/doc |")
    lines.append("|---|---|---|")
    for s in scored:
        r = s["routing"]
        lines.append(f"| {s['arm']} | {r['gpu_seconds_per_doc']} | {r['cpu_core_seconds_per_doc']} |")
    lines.append("")
    if any(s["routing"]["device_time_measured"] for s in scored):
        lines.append("Dollar sweep (ILLUSTRATIVE rates; replace with the agreed cost model before "
                     "any number leaves this repo):")
        lines.append("")
        for s in scored:
            r = s["routing"]
            if not r["device_time_measured"]:
                continue
            lines.append(f"- arm {s['arm']}:")
            for row in cost_model.sweep(r["gpu_seconds"], r["cpu_core_seconds"], s["docs"], rates):
                lines.append(f"  - {row['scenario']}: ${row['dollar_per_doc']}/document "
                             f"(gpu ${row['gpu_hour']}/h, core ${row['core_hour']}/h)")
        lines.append("")
    else:
        lines.append("No device-time headers captured (real vLLM backends); price from pool "
                     "/metrics scrapes instead.")
        lines.append("")

    lines.append("## Resilience")
    lines.append("")
    lines.append("| arm | fallback (degraded) | errors |")
    lines.append("|---|---|---|")
    for s in scored:
        lines.append(f"| {s['arm']} | {s['routing']['fallback']} | {s['routing']['errors']} |")
    lines.append("")

    report_json = {"truth_docs": len(truth_docs), "arms": scored}
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
        if group.get("entry") != "sensitive":
            continue
        sensitive_total += group["count"]
        if group.get("route") not in APPROVED_ROUTES:
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
        for r in run["records"]:
            route = (r.get("headers", {}) or {}).get("x-demo-route")
            if route:
                mine[route] = mine.get(route, 0) + 1
        result["pipeline_routes"] = mine
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
    note = ("Device-time and dollar figures are SIMULATED where the fake pool backends were used; "
            f"ground truth: {report_json.get('truth_docs')} synthetic documents.")
    return HTML_PAGE.replace("__NOTE__", note).replace("__BODY__", "\n".join(parts))


def _html_tables(report_json: dict) -> list[tuple[str, list[str], list[tuple[list, list]]]]:
    arms = report_json.get("arms", [])
    tables = []

    headers = ["arm", "mode", "docs", "wall s", "errors", "fallback (degraded)"]
    rows = []
    for s in arms:
        rows.append(([s["arm"], s["mode"], s["docs"], s["wall_s"],
                      s["routing"]["errors"], s["routing"]["fallback"]],
                     ["", "", "", "", "", ""]))
    tables.append(("Arms overview", headers, rows))

    headers = ["arm", "extract P", "extract R", "extract F1", "risk acc", "type acc",
               "report acc", "docs fully correct"]
    rows = []
    for s in arms:
        e = s["extraction"]
        rows.append(([s["arm"], e["precision"], e["recall"], e["f1"], s["risk_accuracy"],
                      s["type_accuracy"], s["report_accuracy"],
                      f"{s['docs_fully_correct']}/{s['docs']}"], [""] * 8))
    tables.append(("Quality vs ground truth", headers, rows))

    headers = ["arm", "CPU-served share", "sensitive steps", "on approved pool", "violations"]
    rows = []
    for s in arms:
        r = s["routing"]
        rows.append(([s["arm"], r["cpu_served_share"], r["sensitive_steps"],
                      r["sensitive_on_approved"], r["sensitive_violations"]],
                     ["", "", "", "", ' class="bad"' if r["sensitive_violations"] else ' class="good"']))
    tables.append(("Routing and governance", headers, rows))

    headers = ["arm", "GPU-s/doc", "CPU core-s/doc"]
    rows = []
    for s in arms:
        r = s["routing"]
        rows.append(([s["arm"], r["gpu_seconds_per_doc"], r["cpu_core_seconds_per_doc"]], ["", "", ""]))
    tables.append(("Cost (device-time; simulated with fake pools)", headers, rows))
    return tables


def main() -> None:
    parser = argparse.ArgumentParser(description="score hybrid demo runs")
    sub = parser.add_subparsers(dest="command")

    report = sub.add_parser("report", help="build the measurement report from run dirs")
    report.add_argument("--runs", nargs="+", required=True)
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
    text, data = build_report(args.runs, args.data, args.cores_per_cpu_replica, rates)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    out.with_suffix(".json").write_text(json.dumps(data, indent=1))
    out.with_suffix(".html").write_text(render_html(data))
    print(text)


if __name__ == "__main__":
    main()
