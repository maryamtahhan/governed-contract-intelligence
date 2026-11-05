"""Live pool-metrics sampling for measured device-time accounting.

Real vLLM does not report "busy seconds", so the sampler scrapes each pool's
/metrics endpoint during a run and integrates what the pools do expose:

- real vLLM: ``vllm:num_requests_running`` / ``vllm:num_requests_waiting``
  gauges -> device_active_seconds = time with any request in flight (the
  device is occupied and cannot be shared away), request_seconds = integral
  of in-flight requests; token counters give prompt/generation totals.
- simulated pools (hybrid-pool): ``pool_inflight`` gauges and the exact
  ``pool_busy_seconds_total`` counter -> device time comes from the counter
  delta, marked as exact rather than integrated.

The scorer prefers metrics.json (measured) over the simulated pools'
X-Busy-Ms response headers and labels the source in the report.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request
from typing import Any

LINE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9.eE+-]+)")


def _gauge(metrics: dict[str, float], name: str) -> float:
    """vLLM exports colon-style names (vllm:num_requests_running); the Python
    prometheus client can only emit underscores - accept either."""
    return metrics.get(name, metrics.get(name.replace(":", "_"), 0.0))


def parse_prometheus(text: str) -> dict[str, float]:
    """Sum values per metric name (label sets merged)."""
    values: dict[str, float] = {}
    for line in text.splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        name, _, raw = m.groups()
        try:
            values[name] = values.get(name, 0.0) + float(raw)
        except ValueError:
            continue
    return values


class MetricsSampler(threading.Thread):
    """Scrape pool metrics URLs in the background until stopped."""

    def __init__(self, pools: dict[str, str], interval: float = 2.0) -> None:
        super().__init__(daemon=True)
        self.pools = pools
        self.interval = interval
        self._stop_event = threading.Event()
        self.samples: dict[str, list[tuple[float, dict[str, float]]]] = {p: [] for p in pools}
        self.errors: dict[str, int] = {p: 0 for p in pools}

    def _scrape(self, url: str) -> dict[str, float]:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return parse_prometheus(resp.read().decode("utf-8", "replace"))

    def run(self) -> None:
        while not self._stop_event.is_set():
            now = time.monotonic()
            for name, url in self.pools.items():
                try:
                    self.samples[name].append((now, self._scrape(url)))
                except OSError:
                    self.errors[name] += 1
            self._stop_event.wait(self.interval)

    def stop_and_summary(self) -> dict[str, Any]:
        self._stop_event.set()
        self.join(timeout=10)
        return {name: self._summarize(name, samples)
                for name, samples in self.samples.items()}

    def _summarize(self, name: str, samples: list[tuple[float, dict[str, float]]]) -> dict[str, Any]:
        if not samples:
            return {"samples": 0, "scrape_errors": self.errors.get(name, 0)}
        first_t, first = samples[0]
        last_t, last = samples[-1]
        elapsed = last_t - first_t
        summary: dict[str, Any] = {"samples": len(samples), "elapsed_s": round(elapsed, 1),
                                   "scrape_errors": self.errors.get(name, 0)}

        def counter_delta(metric: str) -> float | None:
            if metric in first and metric in last:
                return round(max(0.0, last[metric] - first[metric]), 1)
            return None

        gauge_names = ("vllm:num_requests_running", "vllm_num_requests_running",
                       "vllm:num_requests_waiting", "vllm_num_requests_waiting")
        if any(name in last for name in gauge_names):
            summary["method"] = ("integrated (in-flight gauges; batching means the "
                                 "device serves several requests while occupied)")
            device_active = 0.0
            request_seconds = 0.0
            for (t0, s0), (t1, s1) in zip(samples, samples[1:], strict=False):
                dt = t1 - t0
                inflight0 = _gauge(s0, "vllm:num_requests_running") + _gauge(s0, "vllm:num_requests_waiting")
                inflight1 = _gauge(s1, "vllm:num_requests_running") + _gauge(s1, "vllm:num_requests_waiting")
                if inflight0 > 0 or inflight1 > 0:
                    device_active += dt
                request_seconds += dt * (_gauge(s1, "vllm:num_requests_running")
                                         + _gauge(s0, "vllm:num_requests_running")) / 2.0
            summary["device_active_s"] = round(device_active, 2)
            summary["request_seconds"] = round(request_seconds, 2)
            summary["prompt_tokens"] = counter_delta("vllm:prompt_tokens_total") or \
                counter_delta("vllm_prompt_tokens_total")
            summary["generation_tokens"] = counter_delta("vllm:generation_tokens_total") or \
                counter_delta("vllm_generation_tokens_total")
        else:
            summary["method"] = "exact (pool_busy_seconds_total counter)"
            summary["device_active_s"] = counter_delta("pool_busy_seconds_total")
            summary["request_seconds"] = counter_delta("pool_busy_seconds_total")
        return summary
