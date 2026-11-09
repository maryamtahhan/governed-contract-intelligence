from __future__ import annotations

import json

from hybrid_demo.evalkit.cost import price, sweep
from hybrid_demo.evalkit.score import (
    APPROVED_ROUTES,
    docs_fully_correct,
    extraction_scores,
    gpu_device_seconds,
    routing_scores,
    text_matches,
)


def record(step, doc, found, text, route=None, sensitive=False, fallback=None, busy=None,
           entry=None):
    return {
        "step": step,
        "doc_id": doc,
        "entry": entry,
        "true_complexity": "SIMPLE",
        "true_sensitive": sensitive,
        "headers": {
            "x-demo-route": route,
            "x-praxis-fallback": fallback,
            "x-busy-ms": busy,
        },
        "content": {"found": found, "text": text},
        "outcome": "served",
    }


TRUTH = {
    "d1": {"clauses": {"termination": "Either party may terminate", "governing_law": None},
           "risk": "low", "type": "MSA"},
    "d2": {"clauses": {"termination": "Ninety days notice", "governing_law": "Ireland"},
           "risk": "high", "type": "NDA"},
}


def test_text_matches():
    assert text_matches("Either party may terminate.", "either party may terminate")
    assert text_matches("Either party may terminate this agreement", "Either party may terminate")
    assert not text_matches(None, "x")
    assert not text_matches("a", "b")


def test_extraction_scoring():
    records = [
        record("extract:termination", "d1", True, "Either party may terminate"),
        record("extract:governing_law", "d1", False, None),
        record("extract:termination", "d2", False, None),
        record("extract:governing_law", "d2", True, "wrong law"),
    ]
    scores = extraction_scores(records, TRUTH)
    assert scores["fields_total"] == 4
    assert scores["fields_correct"] == 2
    assert scores["precision"] == 0.5
    assert scores["recall"] == 0.5


def test_routing_scores_governance_and_cost():
    records = [
        record("extract:termination", "d1", True, "x", route="vllm-cpu"),
        record("extract:termination", "d1", True, "x", route="vllm-cpu", sensitive=True),
        record("risk", "d2", True, "x", route="vllm-gpu-approved", sensitive=True, busy="1000"),
        record("risk", "d2", True, "x", route="vllm-gpu", sensitive=True),
        record("extract:termination", "d2", True, "x", route="vllm-cpu", fallback="true", busy="500"),
    ]
    scores = routing_scores(records, docs_in_run=2, cores_per_cpu_replica=8)
    assert scores["routes"]["vllm-cpu"] == 3
    assert scores["sensitive_steps"] == 3
    assert scores["sensitive_on_approved"] == 2
    assert scores["sensitive_violations"] == 1
    assert scores["fallback"] == 1
    assert scores["gpu_seconds"] == 1.0
    assert scores["cpu_core_seconds"] == 0.5 * 8
    assert scores["cpu_served_share"] == 0.6


def test_termination_on_a_judgment_entry_is_not_scored_as_judgment():
    """Termination remains extraction even if a run records a judgment entry."""
    records = [
        record("extract:termination", "d1", True, "x", route="vllm-gpu",
               entry="judgment"),
        record("risk", "d1", True, "x", route="vllm-gpu", entry="judgment"),
        record("report", "d1", True, "x", route="vllm-gpu-approved",
               entry="sensitive-judgment"),
    ]
    scores = routing_scores(records, docs_in_run=1, cores_per_cpu_replica=8)
    assert scores["judgment_steps"] == 2
    assert scores["judgment_on_gpu"] == 2
    assert scores["termination_routes"] == {"vllm-gpu": 1}


def test_approved_routes_definition():
    assert "vllm-cpu" in APPROVED_ROUTES
    assert "vllm-gpu-approved" in APPROVED_ROUTES
    assert "vllm-gpu" not in APPROVED_ROUTES
    assert "vllm-gpu-small" not in APPROVED_ROUTES


def test_cost_math():
    dollars = price(3600.0, 3600.0, gpu_hour=1.0, core_hour=0.1)
    assert abs(dollars - 1.1) < 1e-9
    rows = sweep(3600.0, 7200.0, docs=10, rates={
        "cloud": {"gpu_hour": 1.0, "core_hour": 0.1},
        "owned": {"gpu_hour": 0.5, "core_hour": 0.02, "utilisation": [0.5]},
    })
    assert len(rows) == 2
    assert rows[0]["dollar_per_doc"] == round((1.0 + 0.2) / 10, 4)
    assert rows[1]["gpu_hour"] == 1.0


def test_routing_scores_prefers_measured_metrics():
    records = [
        record("extract:termination", "d1", True, "x", route="vllm-cpu", busy="1000"),
        record("report", None, True, "x", route="vllm-gpu", busy="2000"),
    ]
    metrics = {
        "vllm-cpu": {"device_active_s": 10.0, "method": "exact (counter)"},
        "vllm-gpu": {"device_active_s": 5.0, "method": "exact (counter)"},
    }
    scores = routing_scores(records, docs_in_run=2, cores_per_cpu_replica=8, metrics=metrics)
    assert scores["gpu_seconds"] == 5.0
    assert scores["cpu_core_seconds"] == 80.0
    assert scores["cost_method"].startswith("measured")
    header = routing_scores(records, docs_in_run=2, cores_per_cpu_replica=8)
    assert header["gpu_seconds"] == 2.0
    assert "simulated" in header["cost_method"]


def test_shared_gpu_backend_is_counted_once():
    records = [record("report", None, True, "x", route="vllm-gpu")]
    metrics = {
        "vllm-gpu": {"device_active_s": 5.0},
        "vllm-gpu-approved": {"device_active_s": 4.0},
    }
    pools = {"vllm-gpu-approved": {"shared_with": "vllm-gpu"}}
    scores = routing_scores(records, docs_in_run=1, cores_per_cpu_replica=8,
                            metrics=metrics, pools=pools)
    assert scores["gpu_seconds"] == 5.0
    assert gpu_device_seconds(metrics, pools) == 5.0
    # Older summaries carry only the deploy-time string; use it as the fallback.
    parsed = routing_scores(
        records, docs_in_run=1, cores_per_cpu_replica=8, metrics=metrics,
        pools={"vllm-gpu-approved": {"deployment": "gpumoe-vllm (shared with vllm-gpu)"}})
    assert parsed["gpu_seconds"] == 5.0
    # Historical reports are still reproducible when no sharing is recorded.
    legacy = routing_scores(records, docs_in_run=1, cores_per_cpu_replica=8, metrics=metrics)
    assert legacy["gpu_seconds"] == 9.0


def test_majority_risk_baseline():
    from hybrid_demo.evalkit.score import majority_risk_baseline

    truth = {f"d{i}": {"risk": r} for i, r in enumerate(["low"] * 8 + ["high"] * 10 + ["medium"] * 2)}
    assert majority_risk_baseline(truth, set(truth)) == 0.5
    assert majority_risk_baseline(truth, set()) is None


def test_report_compares_governing_law_by_jurisdiction_and_accepts_any_high_risk_ref():
    from hybrid_demo.evalkit.score import jurisdiction, report_checks

    truth = {
        "d1": {"doc_id": "d1", "risk": "high",
               "clauses": {"governing_law": "the laws of the State of New York"}},
        "d2": {"doc_id": "d2", "risk": "high",
               "clauses": {"governing_law": "COMMONWEALTH OF PENNSYLVANIA"}},
    }
    records = [
        record("extract:governing_law", "d1", True, "New York law"),
        record("extract:governing_law", "d2", True, "Pennsylvania law"),
        {
            "step": "report", "doc_id": None, "outcome": "served",
            "content": {
                "documents": 2,
                "risk_counts": {"high": 2, "medium": 0, "low": 0},
                # Models answer with law names; truth stores the whole clause.
                "governing_law_counts": {"New York": 1, "Pennsylvania": 1},
                # d1 is the alphabetical "highest", but any truth-high doc is a
                # valid reference.
                "highest_risk_ref": "d2",
            },
        },
    ]
    result = report_checks(records, truth)
    assert result["expected"]["governing_law_counts"] == {"new york": 1, "pennsylvania": 1}
    assert result["checks"]["governing_law_counts"] == 1.0
    assert result["checks"]["highest_risk_ref"] == 1.0
    assert result["score"] == 1.0
    assert jurisdiction("laws of West Virginia") == "west virginia"
    assert jurisdiction("Indiana") == "indiana"
    assert jurisdiction("no law named") is None


def test_sampler_parsing_and_integration():
    from hybrid_demo.evalkit.sampler import MetricsSampler, parse_prometheus

    text = """
# HELP vllm:num_requests_running gauge
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running 2.0
vllm:num_requests_waiting 1.0
vllm:prompt_tokens_total 100.0
vllm:generation_tokens_total 40.0
pool_inflight{pool="vllm-cpu",replica="r"} 3
pool_busy_seconds_total{pool="vllm-cpu",replica="r"} 12.5
"""
    values = parse_prometheus(text)
    assert values["vllm:num_requests_running"] == 2.0
    assert values["pool_inflight"] == 3
    assert values["pool_busy_seconds_total"] == 12.5

    sampler = MetricsSampler({"p": "http://127.0.0.1:1/metrics"}, interval=0.05)
    samples = [(0.0, {"vllm:num_requests_running": 2.0, "vllm:num_requests_waiting": 0.0,
                      "vllm:prompt_tokens_total": 10.0, "vllm:generation_tokens_total": 4.0}),
               (2.0, {"vllm:num_requests_running": 0.0, "vllm:num_requests_waiting": 0.0,
                      "vllm:prompt_tokens_total": 110.0, "vllm:generation_tokens_total": 44.0}),
               (4.0, {"vllm:num_requests_running": 0.0, "vllm:num_requests_waiting": 0.0,
                      "vllm:prompt_tokens_total": 110.0, "vllm:generation_tokens_total": 44.0})]
    summary = sampler._summarize("p", samples)
    assert summary["device_active_s"] == 2.0
    assert summary["request_seconds"] == 2.0
    assert summary["prompt_tokens"] == 100.0
    assert summary["generation_tokens"] == 40.0
    assert "integrated" in summary["method"]


def test_sampler_accepts_underscore_only_gauges():
    """The Python prometheus client (our specialist) emits underscore names.

    Regression: the sampler used to detect gauges only by the colon spelling,
    so a pool that was idle at the final scrape fell through to the counter
    path and reported no device time at all.
    """
    from hybrid_demo.evalkit.sampler import MetricsSampler

    sampler = MetricsSampler({"cpu": "http://127.0.0.1:1/metrics"}, interval=0.05)
    samples = [(0.0, {"vllm_num_requests_running": 2.0}),
               (2.0, {"vllm_num_requests_running": 0.0}),
               (4.0, {"vllm_num_requests_running": 0.0})]
    summary = sampler._summarize("cpu", samples)
    assert "integrated" in summary["method"]
    assert summary["device_active_s"] == 2.0


def test_direct_run_metrics_file_is_not_treated_as_device_time(tmp_path):
    """Direct runs write quality scores to metrics.json.

    The sampler summary is a pool -> summary dict; scoring a direct run must
    not feed the former to routing_scores (regression: AttributeError:
    'float' object has no attribute 'get' at report time).
    """
    from hybrid_demo.evalkit.score import load_run, score_run

    run = tmp_path / "cpu-moe"
    run.mkdir()
    records = [{
        "step": "extract:governing_law", "doc_id": "d1",
        "content": {"found": True, "text": "laws of New York"},
        "headers": {}, "outcome": "served",
    }]
    (run / "results.json").write_text(json.dumps({
        "summary": {"arm": "direct", "mode": "direct", "docs": 1, "wall_s": 1.0},
        "records": records,
    }))
    (run / "metrics.json").write_text(json.dumps({
        "extraction": {"precision": 1.0, "recall": 1.0, "f1": 1.0,
                       "fields_correct": 1, "fields_total": 1},
        "risk_accuracy": 1.0,
        "report_accuracy": 0.5,
        "docs_fully_correct": 1,
    }))
    truth = {"d1": {"clauses": {"governing_law": "laws of New York"}}}
    scored = score_run(load_run(run), truth, cores_per_cpu_replica=8)
    # Direct runs are labelled by pool directory so three pools can share a
    # report without three rows called "direct".
    assert scored["arm"] == "cpu-moe"
    assert scored["routing"]["gpu_time_measured"] is False
    assert scored["routing"]["cpu_time_measured"] is False
    assert scored["routing"]["cost_method"].startswith("unmeasured")


def test_dataset_provenance_comes_from_the_manifest():
    from hybrid_demo.evalkit.score import dataset_description

    synthetic = dataset_description({"source": "synthetic", "count": 12, "seed": 42})
    assert "12 synthetic documents" in synthetic
    assert "no personal data" in synthetic
    assert "CUAD" not in synthetic

    cuad = dataset_description({"source": "cuad", "count": 40, "seed": 42})
    assert "CUAD v1 real contracts" in cuad
    assert "CC BY 4.0" in cuad
    assert "synthetic documents" not in cuad


def _run_dir(tmp_path, records):
    import json

    run = tmp_path / "run"
    run.mkdir()
    (run / "results.json").write_text(json.dumps({
        "records": records,
        "summary": {"arm": "B", "mode": "hybrid", "docs": 1, "wall_s": 1.0},
    }))
    return run


def test_report_names_containment_and_flags_a_weak_classifier(tmp_path):
    import json

    from hybrid_demo.evalkit.score import build_report

    data = tmp_path / "data"
    data.mkdir()
    (data / "ground_truth.json").write_text(json.dumps({
        "source": "synthetic", "count": 1, "seed": 42,
        "documents": [{"doc_id": "d1", "type": "MSA", "risk": "low",
                       "clauses": {"termination": "Either party may terminate"}}],
    }))
    run = _run_dir(tmp_path, [{
        "step": "classify", "doc_id": "d1",
        "true_complexity": "SIMPLE", "true_sensitive": False,
        "headers": {"x-demo-classification": "REASONING", "x-demo-route": "vllm-cpu"},
        "content": {"type": "REASONING"},
        "outcome": "served",
    }])
    text, report = build_report([str(run)], str(data), cores_per_cpu_replica=4)
    assert "containment" in text
    assert "span-exact" in text
    assert "Classifier caveat" in text
    assert report["dataset"] == (
        "1 synthetic documents (seed 42, licences: synthetic, no personal data)")
    assert report["arms"][0]["classifier"]["accuracy"] == 0.0


def test_report_accepts_the_documented_repeated_runs_form(tmp_path, monkeypatch):
    """The handoff command uses `--runs A --runs B`; it must not drop runs."""
    import sys

    from hybrid_demo.evalkit import score

    seen: dict[str, list[str]] = {}

    def fake_build(run_dirs, data_dir, cores, rates):
        seen["runs"] = run_dirs
        return "report", {"arms": []}

    monkeypatch.setattr(score, "build_report", fake_build)
    monkeypatch.setattr(score, "render_html", lambda data: "html")
    monkeypatch.setattr(sys, "argv", [
        "hybrid-score", "report", "--runs", "one", "--runs", "two",
        "--data", "data/synthetic", "--out", str(tmp_path / "report.md"),
    ])
    score.main()
    assert seen["runs"] == ["one", "two"]


class _FakeSummary:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_governance_check_refuses_a_truncated_trace_window(tmp_path, monkeypatch):
    from hybrid_demo.evalkit import score

    records = [{
        "step": "extract:termination", "doc_id": "d1",
        "true_complexity": "SIMPLE", "true_sensitive": True,
        "headers": {"x-demo-route": "vllm-cpu"},
        "content": {"found": False, "text": None},
        "outcome": "served",
    } for _ in range(5)]
    run = _run_dir(tmp_path, records)

    def fake_get(*args, **kwargs):
        return _FakeSummary({
            "total": 7,
            "routes": {"vllm-cpu": 2},
            "groups": [{"entry": "sensitive", "route": "vllm-cpu", "count": 2}],
        })

    monkeypatch.setattr(score.httpx, "get", fake_get)
    result = score.governance_check("http://trace.invalid", str(run))
    assert result["pipeline_sensitive_steps"] == 5
    assert result["trace_sensitive_steps"] == 2
    assert result["trace_window_complete"] is False
    assert result["clean"] is False
    assert "retained 2 of 5" in result["reason"]

    monkeypatch.setattr(score.httpx, "get", lambda *a, **k: _FakeSummary({
        "total": 5,
        "routes": {"vllm-cpu": 5},
        "groups": [{"entry": "sensitive", "route": "vllm-cpu", "count": 5}],
    }))
    complete = score.governance_check("http://trace.invalid", str(run))
    assert complete["trace_window_complete"] is True
    assert complete["clean"] is True
    assert "reason" not in complete


def test_governance_check_flags_a_sensitive_trace_on_an_unapproved_route(monkeypatch):
    from hybrid_demo.evalkit import score

    monkeypatch.setattr(score.httpx, "get", lambda *a, **k: _FakeSummary({
        "total": 1,
        "routes": {"vllm-gpu": 1},
        "groups": [{"entry": "sensitive", "route": "vllm-gpu", "count": 1}],
    }))
    result = score.governance_check("http://trace.invalid")
    assert result["clean"] is False
    assert result["sensitive_entry_violations"] == [
        {"entry": "sensitive", "route": "vllm-gpu", "count": 1}]


def test_a_failed_call_is_not_a_correct_absence():
    """A 503 never answered, so it cannot have said "no clause".

    Regression: both scorers looked only at ``found``, so an extract that timed
    out on an annotated-absent clause counted as correct — and on arm-D-moe
    five such documents were reported fully correct when one was.
    """
    terminated = record("extract:termination", "d1", True,
                        "Either party may terminate")
    risky = {"step": "risk", "doc_id": "d1", "outcome": "served",
             "content": {"risk": "low"}}

    failed = {"step": "extract:governing_law", "doc_id": "d1",
              "outcome": "http_503", "content": {}}
    scores = extraction_scores([terminated, failed], TRUTH)
    assert scores["fields_correct"] == 1
    assert docs_fully_correct([terminated, failed, risky], TRUTH) == 0

    # The same document answers "no clause" when the call is served.
    absent = record("extract:governing_law", "d1", False, None)
    assert extraction_scores([terminated, absent], TRUTH)["fields_correct"] == 2
    assert docs_fully_correct([terminated, absent, risky], TRUTH) == 1
