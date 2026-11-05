#!/usr/bin/env python3
"""A local stand-in for praxis-ai-llm-d-sc, used only by the test suite.

It parses the SAME praxis YAML configs the Linux and OpenShift stacks use
(deploy/linux/praxis/*.yaml) and reproduces the externally visible contract:

- listeners and filter chains per config (general :8080, sensitive :8083)
- label -> cluster routing from the llm_d_sc filter's routes (labels come from
  hybrid_demo.classifier.classify_complexity, standing in for the real
  classifier model)
- X-Llm-D-Sc-Label / X-Llm-D-Sc-Latency-Us request headers (the trace-ui
  relays turn these into X-Demo-Classification and per-hop classifier timing)
- failover per the config's failover step, adding X-Praxis-Fallback: true
- pin and round-robin chains for arms A and C
- the static /v1/models response

It is a test double, not a gateway: no health polling (each request attempts
its endpoints directly), no circuit breakers, no streaming.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import time

import httpx
import uvicorn
import yaml
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse


def _register_lb_clusters(filters: list, clusters: dict, endpoint_map: dict[str, str]) -> None:
    for sf in filters:
        if sf.get("filter") != "load_balancer":
            continue
        for c in sf.get("clusters", []):
            clusters.setdefault(
                c["name"],
                {"endpoints": [endpoint_map.get(e, e) for e in c.get("endpoints", [])]},
            )


def parse_config(path: str, endpoint_map: dict[str, str]) -> dict:
    """Extract the routing skeleton the stub understands from a praxis config."""
    raw = yaml.safe_load(open(path))
    clusters = {}
    for cluster in raw.get("clusters", []):
        endpoints = [endpoint_map.get(e, e) for e in cluster.get("endpoints", [])]
        clusters[cluster["name"]] = {"endpoints": endpoints}

    chains = {}
    for chain in raw.get("filter_chains", []):
        name = chain["name"]
        parsed = {"kind": "unknown", "routes": {}, "failover": None, "pin": None}
        for f in chain.get("filters", []):
            if f.get("filter") == "router" and parsed["kind"] == "unknown":
                routes = f.get("routes", [])
                if routes:
                    parsed["kind"] = "pin"
                    parsed["pin"] = routes[0].get("cluster")
            elif f.get("filter") == "iterative_request_router":
                parsed["kind"] = "iterative"
                for step in f.get("steps", []):
                    if step.get("name") == "classify-and-route":
                        for sf in step.get("filters", []):
                            if sf.get("filter") == "llm_d_sc":
                                for r in sf.get("routes", []):
                                    parsed["routes"][r["label"]] = r["cluster"]
                            elif sf.get("filter") == "load_balancer":
                                _register_lb_clusters(step.get("filters", []), clusters, endpoint_map)
                    elif step.get("name") == "failover":
                        for sf in step.get("filters", []):
                            if sf.get("filter") == "router":
                                for r in sf.get("routes", []):
                                    parsed["failover"] = r.get("cluster")
                            elif sf.get("filter") == "load_balancer":
                                _register_lb_clusters(step.get("filters", []), clusters, endpoint_map)
        if parsed["kind"] == "unknown" and not parsed["routes"]:
            parsed["kind"] = "pin"
            parsed["pin"] = "vllm-any"
        chains[name] = parsed

    listeners = {}
    for listener in raw.get("listeners", []):
        address = listener["address"]
        port = int(re.search(r":(\d+)$", address).group(1))
        chain_names = listener.get("filter_chains", [])
        listeners[port] = chains.get(chain_names[0]) if chain_names else None
    return {"listeners": listeners, "clusters": clusters}


class StubState:
    def __init__(self, parsed: dict) -> None:
        self.parsed = parsed
        self.client = httpx.AsyncClient(timeout=120.0)
        self.rr: dict[str, int] = {}

    def pick_endpoint(self, cluster: str) -> str | None:
        endpoints = self.parsed["clusters"].get(cluster, {}).get("endpoints", [])
        if not endpoints:
            return None
        if len(endpoints) == 1:
            return endpoints[0]
        self.rr[cluster] = self.rr.get(cluster, 0) + 1
        return endpoints[self.rr[cluster] % len(endpoints)]


def create_stub_app(state: StubState, chain: dict) -> FastAPI:
    from hybrid_demo.classifier import classify_complexity

    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/v1/models")
    async def models():
        return JSONResponse(
            content={"object": "list",
                     "data": [{"id": "Semantic-Qwen", "object": "model", "created": 0,
                               "owned_by": "praxis"}]}
        )

    @app.post("/v1/chat/completions")
    async def completions(request: Request, body: dict = Body(...)):
        prompt = "\n".join(
            m.get("content", "") for m in body.get("messages", [])
            if isinstance(m, dict) and isinstance(m.get("content"), str)
        )
        # Pin chains have no llm_d_sc filter in the real config (arms A/C and
        # the judgment entries), so the stub must not invent a label for them.
        label = None
        classifier_us = None
        if chain["kind"] == "iterative":
            t0 = time.perf_counter_ns()
            label = classify_complexity(prompt)
            classifier_us = (time.perf_counter_ns() - t0) // 1000 + 150

        if chain["kind"] == "pin":
            attempts = [chain["pin"], chain["failover"]]
            attempts = [a for a in attempts if a]
        else:
            primary = chain["routes"].get(label, "vllm-cpu")
            attempts = [primary] + ([chain["failover"]] if chain["failover"] and
                                    chain["failover"] != primary else [])

        forward_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in {"host", "content-length", "connection", "accept-encoding"}
        }
        if label is not None:
            forward_headers["X-Llm-D-Sc-Label"] = label
            forward_headers["X-Llm-D-Sc-Latency-Us"] = str(classifier_us)
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query
        last_error = "no endpoint"
        for cluster in attempts:
            endpoints = list(state.parsed["clusters"].get(cluster, {}).get("endpoints", []))
            if not endpoints:
                last_error = f"cluster {cluster} has no endpoints"
                continue
            if len(endpoints) > 1:
                state.rr[cluster] = state.rr.get(cluster, 0) + 1
                start = state.rr[cluster] % len(endpoints)
                endpoints = endpoints[start:] + endpoints[:start]
            for endpoint in endpoints:
                # Mirror praxis: the failover step starts with empty headers,
                # so a failover attempt carries no classifier label.
                attempt_headers = dict(forward_headers)
                if cluster != attempts[0]:
                    for key in list(attempt_headers):
                        if key.lower().startswith("x-llm-d-sc"):
                            del attempt_headers[key]
                try:
                    resp = await state.client.post(
                        endpoint + path, json=body, headers=attempt_headers
                    )
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue
                if resp.status_code >= 500:
                    last_error = f"upstream {resp.status_code}"
                    continue
                out = JSONResponse(content=resp.json(), status_code=resp.status_code)
                for key, value in resp.headers.items():
                    if key.lower() in {"content-length", "transfer-encoding", "content-type",
                                       "server", "date", "connection"}:
                        continue
                    out.headers[key] = value
                if cluster != attempts[0]:
                    out.headers["X-Praxis-Fallback"] = "true"
                return out
        return JSONResponse(
            status_code=503,
            content={"error": {"message": f"fail-closed: no available upstream ({last_error})",
                               "type": "upstream_unavailable"}},
        )

    return app


async def serve(parsed: dict, port_map: dict[int, int]) -> None:
    state = StubState(parsed)
    servers = []
    for config_port, chain in parsed["listeners"].items():
        local_port = port_map.get(config_port, config_port)
        app = create_stub_app(state, chain)
        config = uvicorn.Config(app, host="127.0.0.1", port=local_port, log_level="warning")
        servers.append(uvicorn.Server(config))
    await asyncio.gather(*(server.serve() for server in servers))


def main() -> None:
    parser = argparse.ArgumentParser(description="praxis test stub (tests only)")
    parser.add_argument("--config", required=True, help="praxis yaml config to parse")
    parser.add_argument("--endpoint-map", default="",
                        help="map cluster endpoints to local addresses, k=v pairs comma-separated")
    parser.add_argument("--port-map", default="",
                        help="map config listener ports to local ports, k=v pairs comma-separated")
    args = parser.parse_args()
    endpoint_map = dict(
        kv.split("=", 1) for kv in args.endpoint_map.split(",") if "=" in kv
    )
    port_map = {int(k): int(v) for k, v in
                (kv.split("=", 1) for kv in args.port_map.split(",") if "=" in kv)}
    parsed = parse_config(args.config, endpoint_map)
    asyncio.run(serve(parsed, port_map))


if __name__ == "__main__":
    main()
