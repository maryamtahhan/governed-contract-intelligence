"""Telemetry for the demo: recorded run artifacts and live vLLM gauges.

Two sources, kept apart on purpose.

*Recorded* means the files a finished sweep or run left on disk: the
concurrency sweep in ``runs/cuad-test/perf`` and the per-pool counters in each
run's ``metrics.json``. They are facts about runs that already happened, so the
page shows them without a reachable server.

*Live* means vLLM's own ``/metrics`` endpoint, polled from here rather than
from the browser (the pool is not CORS-open, and a rate needs two samples
anyway). vLLM's counters are cumulative per process, so a rate is the change
between two polls; the first poll after the server starts therefore has no
rate, and the page says "first sample" rather than printing a zero.

A pool name is a routing tier. Whether the request reached a CPU or a GPU is
the deployment's business, not this module's, so nothing here infers hardware
from a name.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from pathlib import Path

import httpx

from .payload import _perf, _pool_load

# Gauges and counters this page reads. Missing ones are simply absent: vLLM's
# metric set differs by version and by device, and a CPU build has no cache use.
METRICS = {
    "running": "vllm:num_requests_running",
    "waiting": "vllm:num_requests_waiting",
    "prompt_tokens_total": "vllm:prompt_tokens_total",
    "gen_tokens_total": "vllm:generation_tokens_total",
    "ttft_sum": "vllm:time_to_first_token_seconds_sum",
    "ttft_count": "vllm:time_to_first_token_seconds_count",
    "e2e_sum": "vllm:e2e_request_latency_seconds_sum",
    "e2e_count": "vllm:e2e_request_latency_seconds_count",
    "cache_pct": "vllm:gpu_cache_usage_perc",
}


def metric(text: str, name: str) -> float | None:
    """Sum one Prometheus gauge over vLLM's per-label lines, if present.

    The boundary check matters: ``vllm:num_requests_running`` must not also
    match a longer series that merely starts with those characters.
    """
    total, seen = 0.0, False
    for line in text.splitlines():
        if not line.startswith(name) or line[len(name):len(name) + 1] not in ("{", " "):
            continue
        try:
            value = float(line.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            continue
        # NaN and infinities are legal Prometheus text but not valid JSON, and
        # a 500 from /api/telemetry would take the whole page down with them.
        if not math.isfinite(value):
            continue
        total += value
        seen = True
    return total if seen else None


def _base(url: str) -> str:
    url = url.rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


async def snapshot(client: httpx.AsyncClient, name: str, url: str) -> dict:
    """One pool's state right now, or the error that kept us from reading it."""
    base = _base(url)
    out: dict = {"name": name, "url": base}
    try:
        models = (await client.get(f"{base}/v1/models")).json()
        out["model"] = (models.get("data") or [{}])[0].get("id")
    except (httpx.HTTPError, ValueError, AttributeError) as e:
        out["error"] = f"{e.__class__.__name__}: {e}"
        return out
    try:
        text = (await client.get(f"{base}/metrics")).text
    except httpx.HTTPError as e:
        out["error"] = f"{e.__class__.__name__}: {e}"
        return out
    for key, series in METRICS.items():
        value = metric(text, series)
        if value is not None:
            out[key] = value
    return out


class LivePools:
    """Poll the configured pools and turn cumulative counters into rates."""

    def __init__(self, pools: list[tuple[str, str]], capacity: int = 120):
        self.pools = pools
        self.capacity = capacity
        # Previous cumulative sample per pool; a rate needs two of them.
        self._prev: dict[str, tuple[float, dict]] = {}
        self.history: dict[str, deque] = {n: deque(maxlen=capacity) for n, _ in pools}

    async def sample(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=4.0) as client:
            snaps = await asyncio.gather(
                *(snapshot(client, name, url) for name, url in self.pools),
                return_exceptions=True)
        now = time.time()
        out = []
        for name, snap in zip((n for n, _ in self.pools), snaps, strict=True):
            if isinstance(snap, BaseException):
                snap = {"name": name, "error": f"{snap.__class__.__name__}: {snap}"}
            out.append(self._rate(snap, now))
        return out

    def _rate(self, snap: dict, now: float) -> dict:
        """Attach the interval rates, computed against the previous sample.

        A counter that went backwards means the pool restarted; the interval
        is then not a measurement of anything, so it is dropped rather than
        shown as a negative or a huge spike.
        """
        name = snap["name"]
        prev = self._prev.get(name)
        self._prev[name] = (now, snap)
        if "error" in snap:
            return snap
        if prev is None:
            snap["first_sample"] = True
        # A rate is only meaningful over a real interval; two polls a second
        # apart would report noise with three significant figures.
        else:
            prev_at, before = prev
            dt = now - prev_at
            if dt >= 1.0 and "error" not in before:
                self._adds(snap, before, dt)
        point = {"t": round(now, 1)}
        for key in ("gen_tps", "prompt_tps", "running", "waiting", "avg_ttft_ms"):
            if snap.get(key) is not None:
                point[key] = snap[key]
        self.history[name].append(point)
        snap["history"] = list(self.history[name])[-60:]
        return snap

    @staticmethod
    def _adds(snap: dict, before: dict, dt: float) -> None:
        def delta(key: str) -> float | None:
            a, b = snap.get(key), before.get(key)
            return None if a is None or b is None or a < b else a - b

        gen, prompt = delta("gen_tokens_total"), delta("prompt_tokens_total")
        if gen is not None:
            snap["gen_tps"] = round(gen / dt, 1)
        if prompt is not None:
            snap["prompt_tps"] = round(prompt / dt, 1)
        ttft, ttft_n = delta("ttft_sum"), delta("ttft_count")
        if ttft is not None and ttft_n:
            snap["avg_ttft_ms"] = round(ttft / ttft_n * 1000, 1)
        e2e, e2e_n = delta("e2e_sum"), delta("e2e_count")
        if e2e is not None and e2e_n:
            snap["avg_e2e_ms"] = round(e2e / e2e_n * 1000, 1)


def _run_label(path: Path, runs_dir: Path) -> str:
    """A stable label for one run directory.

    Matrix runs are nested (``runs/<batch>/<config>``), so the directory leaf
    is not unique across archived runs. Use the path relative to the scan root
    for nested runs, and the leaf name for a single run directory.
    """
    try:
        rel = path.resolve().relative_to(runs_dir.resolve())
    except ValueError:
        return path.name
    return rel.name if len(rel.parts) == 1 else rel.as_posix()


def run_artifacts(runs_dir: Path, focus: list[Path]) -> dict:
    """What the recorded runs spent, plus the sweep beside them.

    ``focus`` names the runs the stage page is showing; they come first so the
    telemetry page describes the demo on screen before the archive. The scan is
    recursive because matrix runs live one level below the batch directory.
    """
    runs: list[dict] = []
    seen: set[Path] = set()
    discovered = sorted(
        (p.parent for p in runs_dir.rglob("metrics.json")),
        key=lambda p: p.stat().st_mtime, reverse=True)
    order = [p for p in focus if p.is_dir()] + discovered
    for path in order:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        name = _run_label(path, runs_dir)
        pools = _pool_load(path)
        if not pools:
            continue
        summary_path = path / "summary.json"
        summary = {}
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text())
            except ValueError:
                summary = {}
        active = {n: p for n, p in pools.items()
                  if (p.get("prompt_tokens") or p.get("generation_tokens")
                      or p.get("device_active_s"))}
        runs.append({
            "run": name,
            "path": str(path),
            "arm": summary.get("arm"),
            "hardware": summary.get("hardware"),
            "wall_s": summary.get("wall_s"),
            "docs": summary.get("docs"),
            "pools": pools,
            "active_pools": sorted(active),
        })
    return {
        "perf": _perf(runs_dir / "perf"),
        "runs": runs,
        "generated_at": round(time.time(), 1),
    }
