from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PRAXIS_CONFIGS = REPO_ROOT / "manifests" / "base" / "praxis"
STUB = Path(__file__).parent / "stub_praxis.py"

POOLS = {
    "cuad-specialist": dict(prefill=20, decode=5, concurrency=4,
                            errors="simple=0.0,complex=0.0,reasoning=0.0",
                            model="cuad-specialist"),
    "vllm-cpu": dict(prefill=700, decode=24, concurrency=8,
                     errors="simple=0.10,complex=0.18,reasoning=0.55"),
    "vllm-gpu": dict(prefill=40, decode=12, concurrency=12,
                     errors="simple=0.01,complex=0.04,reasoning=0.03"),
    "vllm-gpu-approved": dict(prefill=40, decode=12, concurrency=12,
                              errors="simple=0.01,complex=0.04,reasoning=0.03"),
    "vllm-gpu-small": dict(prefill=30, decode=5, concurrency=8,
                           errors="simple=0.04,complex=0.30,reasoning=0.50"),
}
RELAY_PORTS = {
    "cuad-specialist": 8088,
    "vllm-cpu": 8081,
    "vllm-gpu": 8082,
    "vllm-gpu-approved": 8084,
    "vllm-gpu-small": 8085,
}
STUB_LISTENER_PORTS = {8080: "general", 8083: "sensitive"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=2.0)
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise TimeoutError(f"not ready: {url}")


class Proc:
    def __init__(self, popen: subprocess.Popen) -> None:
        self.popen = popen

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.popen.args_port}"

    def terminate(self) -> None:
        self.popen.terminate()
        try:
            self.popen.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.popen.kill()
            self.popen.wait()


class LocalStack:
    """Fake pools + trace-ui + the praxis stub, mirroring the compose stack."""

    def __init__(self, tmpdir: Path) -> None:
        self.tmpdir = tmpdir
        self.pools: dict[str, subprocess.Popen] = {}
        self.pool_ports: dict[str, int] = {}
        self.relay_ports: dict[str, int] = {}
        self.stub: subprocess.Popen | None = None
        self.stub_general_port = free_port()
        self.stub_sensitive_port = free_port()
        self.ingress_port = free_port()
        self.sensitive_ingress_port = free_port()

        for name, profile in POOLS.items():
            port = free_port()
            self.pool_ports[name] = port
            self.relay_ports[name] = free_port()
            self.pools[name] = subprocess.Popen(
                [
                    sys.executable, "-m", "hybrid_demo.fakepool.server",
                    "--pool", name, "--model",
                    profile.get("model", "Semantic-Qwen"),
                    "--host", "127.0.0.1", "--port", str(port),
                    "--prefill-ms-per-ktok", str(profile["prefill"]),
                    "--decode-ms-per-tok", str(profile["decode"]),
                    "--concurrency", str(profile["concurrency"]),
                    "--time-scale", "0.02",
                    "--error-rates", profile["errors"],
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            wait_http(f"http://127.0.0.1:{port}/healthz")

        env = dict(os.environ)
        env.update({
            "TRACE_DB_PATH": str(tmpdir / "traces.db"),
            "PRAXIS_UPSTREAM": f"http://127.0.0.1:{self.stub_general_port}",
            "SENSITIVE_UPSTREAM": f"http://127.0.0.1:{self.stub_sensitive_port}",
            "TRACE_POOLS": ",".join(POOLS),
            "TRACE_INGRESS_PORT": str(self.ingress_port),
            "SENSITIVE_INGRESS_PORT": str(self.sensitive_ingress_port),
        })
        for name in POOLS:
            env[f"{name.upper().replace('-', '_')}_UPSTREAM"] = f"http://127.0.0.1:{self.pool_ports[name]}"
            env[f"{name.upper().replace('-', '_')}_RELAY_PORT"] = str(self.relay_ports[name])
        self.trace_ui = subprocess.Popen(
            [sys.executable, "-m", "hybrid_demo.traceui.server"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        wait_http(f"http://127.0.0.1:{self.ingress_port}/_trace/healthz")
        wait_http(f"http://127.0.0.1:{self.sensitive_ingress_port}/_trace/healthz")
        self.start_stub("config.yaml")

    def start_stub(self, config_name: str) -> None:
        self.stop_stub()
        endpoint_map = ",".join(
            f"trace-ui:{RELAY_PORTS[name]}=http://127.0.0.1:{self.relay_ports[name]}"
            for name in POOLS
        )
        port_map = (f"8080={self.stub_general_port},"
                    f"8083={self.stub_sensitive_port}")
        self.stub = subprocess.Popen(
            [
                sys.executable, str(STUB),
                "--config", str(PRAXIS_CONFIGS / config_name),
                "--endpoint-map", endpoint_map,
                "--port-map", port_map,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        import re

        import yaml

        listeners = yaml.safe_load((PRAXIS_CONFIGS / config_name).read_text()).get("listeners", [])
        ports = {int(re.search(r":(\d+)$", item["address"]).group(1)) for item in listeners}
        if 8080 in ports:
            wait_http(f"http://127.0.0.1:{self.stub_general_port}/v1/models")
        if 8083 in ports:
            wait_http(f"http://127.0.0.1:{self.stub_sensitive_port}/v1/models")
        if 8086 in ports:
            wait_http(f"http://127.0.0.1:{self.stub_general_port}/v1/models")
        if 8087 in ports:
            wait_http(f"http://127.0.0.1:{self.stub_general_port}/v1/models")

    def stop_stub(self) -> None:
        if self.stub:
            self.stub.terminate()
            try:
                self.stub.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.stub.kill()
                self.stub.wait()
            self.stub = None
            time.sleep(0.3)

    def kill_pool(self, name: str) -> None:
        self.pools[name].terminate()
        self.pools[name].wait(timeout=10)

    def start_pool(self, name: str) -> None:
        if self.pools[name].poll() is None:
            return
        profile = POOLS[name]
        port = self.pool_ports[name]
        self.pools[name] = subprocess.Popen(
            [
                sys.executable, "-m", "hybrid_demo.fakepool.server",
                "--pool", name, "--model",
                profile.get("model", "Semantic-Qwen"),
                "--host", "127.0.0.1", "--port", str(port),
                "--prefill-ms-per-ktok", str(profile["prefill"]),
                "--decode-ms-per-tok", str(profile["decode"]),
                "--concurrency", str(profile["concurrency"]),
                "--time-scale", "0.02",
                "--error-rates", profile["errors"],
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        wait_http(f"http://127.0.0.1:{port}/healthz")

    def ensure_pools(self) -> None:
        for name in POOLS:
            if self.pools[name].poll() is not None:
                self.start_pool(name)

    @property
    def gateway(self) -> str:
        return f"http://127.0.0.1:{self.ingress_port}"

    @property
    def sensitive_gateway(self) -> str:
        return f"http://127.0.0.1:{self.sensitive_ingress_port}"

    def teardown(self) -> None:
        self.stop_stub()
        self.trace_ui.terminate()
        self.trace_ui.wait(timeout=10)
        for proc in self.pools.values():
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)


@pytest.fixture(scope="session")
def stack(tmp_path_factory):
    stack = LocalStack(tmp_path_factory.mktemp("stack"))
    yield stack
    stack.teardown()


@pytest.fixture(autouse=True)
def restore_pools(stack):
    yield
    stack.ensure_pools()
