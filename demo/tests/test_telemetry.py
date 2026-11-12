from __future__ import annotations

import json

import httpx
import pytest

from hybrid_demo.ui.telemetry import LivePools, _base, metric, run_artifacts, snapshot

# --- metric parsing -------------------------------------------------------

def test_metric_sums_per_label_lines():
    text = (
        "# HELP vllm:num_requests_running Number of requests running.\n"
        "# TYPE vllm:num_requests_running gauge\n"
        'vllm:num_requests_running{engine="0",model_name="m"} 3.0\n'
        'vllm:num_requests_running{engine="1",model_name="m"} 2.0\n'
    )
    assert metric(text, "vllm:num_requests_running") == 5.0


def test_metric_absent_is_none_not_zero():
    """A CPU build has no cache gauge; absent must not read as an empty cache."""
    assert metric('vllm:num_requests_running{engine="0"} 1.0\n',
                  "vllm:gpu_cache_usage_perc") is None


def test_metric_does_not_match_a_longer_series_name():
    text = 'vllm:num_requests_running_total{engine="0"} 9.0\n'
    assert metric(text, "vllm:num_requests_running") is None


def test_metric_skips_values_json_cannot_carry():
    """NaN is legal exposition text and would make the response unparseable."""
    text = ('vllm:prompt_tokens_total{engine="0"} NaN\n'
            'vllm:prompt_tokens_total{engine="1"} +Inf\n'
            'vllm:prompt_tokens_total{engine="2"} 12\n')
    assert metric(text, "vllm:prompt_tokens_total") == 12.0
    assert metric('vllm:prompt_tokens_total{engine="0"} NaN\n',
                  "vllm:prompt_tokens_total") is None


# --- pool URL -------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://p:8000/v1", "http://p:8000/v1/",
                                 "http://p:8000/"])
def test_base_strips_v1_and_trailing_slash(url):
    assert _base(url) == "http://p:8000"


# --- snapshot -------------------------------------------------------------

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_snapshot_reads_model_and_gauges():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen-cpu"}]})
        return httpx.Response(200, text=(
            'vllm:num_requests_running{engine="0"} 2.0\n'
            'vllm:prompt_tokens_total{engine="0"} 1500\n'
            'vllm:generation_tokens_total{engine="0"} 40\n'))

    async with _client(handler) as client:
        snap = await snapshot(client, "cpu", "http://p:8000/v1")
    assert snap["model"] == "qwen-cpu"
    assert snap["url"] == "http://p:8000"
    assert snap["running"] == 2.0
    assert snap["prompt_tokens_total"] == 1500.0
    assert "error" not in snap


async def test_snapshot_unreachable_carries_the_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async with _client(handler) as client:
        snap = await snapshot(client, "gpu", "http://p:8000")
    assert "ConnectError" in snap["error"]
    assert "running" not in snap


# --- rates ----------------------------------------------------------------

def _snap(gen=0.0, prompt=0.0, ttft=(0.0, 0), e2e=(0.0, 0), **extra):
    out = {"name": "cpu", "gen_tokens_total": gen, "prompt_tokens_total": prompt,
           "ttft_sum": ttft[0], "ttft_count": ttft[1],
           "e2e_sum": e2e[0], "e2e_count": e2e[1]}
    out.update(extra)
    return out


def test_first_sample_reports_no_rate():
    """The counters are cumulative, so one sample cannot yield a rate."""
    pools = LivePools([("cpu", "http://p:8000")])
    snap = pools._rate(_snap(gen=100), now=1000.0)
    assert snap["first_sample"] is True
    assert "gen_tps" not in snap


def test_second_sample_is_a_delta_over_the_interval():
    pools = LivePools([("cpu", "http://p:8000")])
    pools._rate(_snap(gen=100, prompt=1000, ttft=(0.5, 2)), now=1000.0)
    snap = pools._rate(_snap(gen=1100, prompt=6000, ttft=(2.5, 4), e2e=(9.0, 4)),
                       now=1010.0)
    assert snap["gen_tps"] == 100.0
    assert snap["prompt_tps"] == 500.0
    assert snap["avg_ttft_ms"] == 1000.0  # (2.5 - 0.5) s over (4 - 2) requests
    assert snap["avg_e2e_ms"] == 2250.0
    assert "first_sample" not in snap


def test_counter_regression_is_a_restart_not_a_spike():
    pools = LivePools([("cpu", "http://p:8000")])
    pools._rate(_snap(gen=5000), now=1000.0)
    snap = pools._rate(_snap(gen=10), now=1010.0)
    assert "gen_tps" not in snap


def test_interval_too_short_for_a_rate():
    """Two polls a second apart would publish noise as a measurement."""
    pools = LivePools([("cpu", "http://p:8000")])
    pools._rate(_snap(gen=100), now=1000.0)
    snap = pools._rate(_snap(gen=900), now=1000.5)
    assert "gen_tps" not in snap


def test_an_interval_that_spans_an_error_is_dropped():
    pools = LivePools([("cpu", "http://p:8000")])
    pools._rate(_snap(gen=100), now=1000.0)
    pools._rate({"name": "cpu", "error": "ConnectError"}, now=1010.0)
    snap = pools._rate(_snap(gen=900), now=1020.0)
    assert "gen_tps" not in snap


def test_history_is_bounded_and_serialisable():
    pools = LivePools([("cpu", "http://p:8000")], capacity=200)
    snap = {}
    for i in range(100):
        snap = pools._rate(_snap(gen=i * 10.0), now=1000.0 + i)
    assert len(snap["history"]) == 60
    assert all(isinstance(p["t"], float) for p in snap["history"])
    json.dumps(snap)


# --- recorded artifacts ---------------------------------------------------

def _write_run(root, name: str, metrics: dict, summary: dict | None = None):
    run = root / name
    run.mkdir(parents=True)
    (run / "metrics.json").write_text(json.dumps(metrics))
    if summary is not None:
        (run / "summary.json").write_text(json.dumps(summary))
    return run


POOL_METRICS = {
    "vllm-cpu": {"samples": 10, "prompt_tokens": 900.0, "generation_tokens": 90.0,
                 "device_active_s": 9.0, "elapsed_s": 10.0, "method": "integrated"},
    "vllm-gpu": {"samples": 10, "prompt_tokens": 0.0, "generation_tokens": 0.0,
                 "device_active_s": 0.0, "elapsed_s": 10.0, "method": "integrated"},
}

SCORED_METRICS = {"extraction": {"f1": 0.69}, "risk_accuracy": 0.63}


def test_run_artifacts_lists_only_runs_with_pool_telemetry(tmp_path):
    _write_run(tmp_path, "through", POOL_METRICS,
               {"arm": "D", "hardware": "sno", "wall_s": 12.0, "docs": 8})
    _write_run(tmp_path, "direct", SCORED_METRICS)
    out = run_artifacts(tmp_path, [])
    assert [r["run"] for r in out["runs"]] == ["through"]
    run = out["runs"][0]
    assert run["arm"] == "D"
    assert run["active_pools"] == ["vllm-cpu"]
    assert run["pools"]["vllm-cpu"]["gen_tps"] == 10.0


def test_run_artifacts_puts_the_focused_runs_first(tmp_path):
    _write_run(tmp_path, "older", POOL_METRICS)
    focus = _write_run(tmp_path, "focused", POOL_METRICS)
    newest = _write_run(tmp_path, "newer", POOL_METRICS)
    out = run_artifacts(tmp_path, [focus])
    names = [r["run"] for r in out["runs"]]
    assert names[0] == "focused"
    assert set(names) == {"focused", "older", "newer"}
    # The archive beside it is most-recent-first, and listed after the focus.
    rest = names[1:]
    mtimes = {p.name: p.stat().st_mtime for p in (tmp_path / "older", newest)}
    assert list(rest) == sorted(rest, key=mtimes.__getitem__, reverse=True)


def test_run_artifacts_discovers_nested_matrix_runs(tmp_path):
    focus = _write_run(tmp_path, "batch/hybrid", POOL_METRICS,
                       {"arm": "D", "wall_s": 12.0, "docs": 8})
    _write_run(tmp_path, "batch/one-pool", POOL_METRICS,
               {"arm": "A", "wall_s": 6.0, "docs": 8})
    out = run_artifacts(tmp_path, [focus])
    names = [r["run"] for r in out["runs"]]
    assert names[0] == "batch/hybrid"
    assert "batch/one-pool" in names


def test_run_artifacts_reads_the_sweep_beside_the_runs(tmp_path):
    perf = tmp_path / "perf"
    perf.mkdir()
    (perf / "cpu-cuad-c4.json").write_text(json.dumps(
        {"num_prompts": 76, "failed": 0, "output_throughput": 39.8,
         "p50_ttft_ms": 900.0}))
    out = run_artifacts(tmp_path, [])
    assert out["perf"][0]["hardware"] == "cpu"
    assert out["perf"][0]["output_tps"] == 39.8
    assert out["perf"][0]["concurrency"] == 4


def test_run_artifacts_tolerates_a_missing_directory(tmp_path):
    out = run_artifacts(tmp_path / "nope", [])
    assert out["runs"] == [] and out["perf"] == []
