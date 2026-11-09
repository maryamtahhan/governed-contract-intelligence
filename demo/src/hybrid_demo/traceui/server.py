#!/usr/bin/env python3
"""A dependency-free OpenAI relay with a small persisted request trace view.

Adopted from RMalone8/llm-d-sc-demo (trace-ui/server.py) and extended for the
hybrid CPU+GPU contract-review demo:

- pools are env-driven (TRACE_POOLS), so the same server serves two pools in a
  minimal deployment or four (cpu, gpu, gpu-approved, gpu-small) in this demo
- a second ingress relay (SENSITIVE_UPSTREAM / SENSITIVE_INGRESS_PORT) feeds
  the gateway's sensitive listener; every trace records which entry it used
- optional judgment ingresses (JUDGMENT_UPSTREAM / JUDGMENT_INGRESS_PORT and
  SENSITIVE_JUDGMENT_UPSTREAM / SENSITIVE_JUDGMENT_INGRESS_PORT) feed the
  stage-aware listeners that carry risk/report to the GPU
- /_trace/api/summary aggregates traces by route, label and entry, which the
  governance check and the measurement harness read as cluster-side evidence
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit
from urllib.request import Request, urlopen

try:
    from . import otel  # installed package (compose: hybrid-traceui)
except ImportError:
    # Standalone (cluster: 02-apply.sh mounts this file from a ConfigMap,
    # with otel.py beside it). Script dir is sys.path[0], so the plain
    # import resolves.
    import otel  # type: ignore[no-redef]

DATABASE = Path(os.environ.get("TRACE_DB_PATH", "/data/traces.db"))
UPSTREAM = os.environ.get("PRAXIS_UPSTREAM", "http://praxis:8080").rstrip("/")
SENSITIVE_UPSTREAM = os.environ.get("SENSITIVE_UPSTREAM", "").rstrip("/")
SENSITIVE_INGRESS_PORT = int(os.environ.get("SENSITIVE_INGRESS_PORT", "8083"))
# Judgment ingresses carry risk + report to the GPU. They are optional so the
# relay still serves stacks whose praxis config predates the judgment
# listeners; the pipeline only uses them when both sides provide them.
JUDGMENT_UPSTREAM = os.environ.get("JUDGMENT_UPSTREAM", "").rstrip("/")
JUDGMENT_INGRESS_PORT = int(os.environ.get("JUDGMENT_INGRESS_PORT", "8086"))
SENSITIVE_JUDGMENT_UPSTREAM = os.environ.get("SENSITIVE_JUDGMENT_UPSTREAM", "").rstrip("/")
SENSITIVE_JUDGMENT_INGRESS_PORT = int(
    os.environ.get("SENSITIVE_JUDGMENT_INGRESS_PORT", "8087")
)
INGRESS_PORT = int(os.environ.get("TRACE_INGRESS_PORT", "8080"))
CONTEXT_PROBE_MARKER = os.environ.get("TRACE_CONTEXT_PROBE_MARKER", "")
MAX_TRACES = int(os.environ.get("TRACE_MAX_TRACES", "250"))
RESPONSE_CAPTURE_BYTES = int(os.environ.get("TRACE_RESPONSE_CAPTURE_BYTES", str(512 * 1024)))
RESPONSE_MAX_CHARS = int(os.environ.get("TRACE_RESPONSE_MAX_CHARS", "16000"))
GPU_LABELS = {"COMPLEX", "REASONING"}
# Sensitive prompts/responses are not stored by default: the audit trail
# records routing metadata, not document text. Set to 1 only for a trusted
# local debugging session.
STORE_SENSITIVE_CONTENT = os.environ.get("TRACE_STORE_SENSITIVE_CONTENT", "") == "1"
# Optional bearer token for the /_trace API and dashboard. Unset keeps the
# laptop demo frictionless; set it on any shared cluster or Route.
API_TOKEN = os.environ.get("TRACE_API_TOKEN", "")
HEALTH_PATH = os.environ.get("TRACE_HEALTH_PATH", "/health")
HEALTH_KEEP = int(os.environ.get("TRACE_HEALTH_KEEP", "300"))
PROBE_FRESH_SECONDS = float(os.environ.get("TRACE_PROBE_FRESH_SECONDS", "20"))
STATE_POLL_SECONDS = float(os.environ.get("TRACE_STATE_POLL_SECONDS", "2"))
STATE_KEEP = int(os.environ.get("TRACE_STATE_KEEP", "2000"))
K8S_API = os.environ.get("KUBERNETES_API", "https://kubernetes.default.svc")
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TRAFFIC_DEPLOYMENT = os.environ.get("TRACE_TRAFFIC_DEPLOYMENT", "praxis-traffic-gen")
TRAFFIC_LABEL = os.environ.get("TRACE_TRAFFIC_LABEL", TRAFFIC_DEPLOYMENT)
DB_LOCK = threading.Lock()
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}

# Each pool: route name -> (upstream, relay port, Deployment, InferenceService).
# A pool is served when its route name is listed in TRACE_POOLS; the upstream
# comes from <ROUTE>_UPSTREAM (dots/dashes become underscores) with the
# predictor-Service default. Only the relays for listed pools are started, so
# the same image serves a two-pool stack or this demo's four pools.
POOL_DEFAULTS = {
    "vllm-cpu": ("http://vllm-cpu-predictor:80", 8081, "vllm-cpu-predictor", "vllm-cpu"),
    "vllm-gpu": ("http://vllm-gpu-predictor:80", 8082, "vllm-gpu-predictor", "vllm-gpu"),
    "vllm-gpu-approved": ("http://vllm-gpu-approved-predictor:80", 8084,
                          "vllm-gpu-approved-predictor", "vllm-gpu-approved"),
    "vllm-gpu-small": ("http://vllm-gpu-small-predictor:80", 8085,
                       "vllm-gpu-small-predictor", "vllm-gpu-small"),
}


def _pool_env(route: str, index: int) -> str:
    env = f"{route.upper().replace('-', '_')}_UPSTREAM"
    default = POOL_DEFAULTS[route][index]
    return os.environ.get(env, default).rstrip("/")


POOLS: dict[str, tuple[str, int, str, str]] = {}
for _route in [p.strip() for p in os.environ.get("TRACE_POOLS", "vllm-cpu,vllm-gpu").split(",") if p.strip()]:
    if _route in POOL_DEFAULTS:
        POOLS[_route] = (
            _pool_env(_route, 0),
            int(os.environ.get(f"{_route.upper().replace('-', '_')}_RELAY_PORT",
                               str(POOL_DEFAULTS[_route][1]))),
            os.environ.get(f"TRACE_{_route.upper().replace('-', '_')}_DEPLOYMENT", POOL_DEFAULTS[_route][2]),
            os.environ.get(f"TRACE_{_route.upper().replace('-', '_')}_ISVC", POOL_DEFAULTS[_route][3]),
        )
HEALTH_ROUTES = tuple(POOLS)


def db() -> sqlite3.Connection:
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE)
    connection.execute("""CREATE TABLE IF NOT EXISTS traces (
        id TEXT PRIMARY KEY, started_at REAL NOT NULL, completed_at REAL,
        method TEXT NOT NULL, path TEXT NOT NULL, request_id TEXT NOT NULL,
        status INTEGER, classification TEXT, route TEXT, fallback INTEGER NOT NULL,
        duration_ms INTEGER, response_headers TEXT NOT NULL DEFAULT '{}', error TEXT,
        question TEXT, response TEXT, response_tokens INTEGER, response_tokens_est INTEGER,
        entry TEXT NOT NULL DEFAULT 'general'
    )""")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(traces)")}
    for column, kind in (("question", "TEXT"), ("response", "TEXT"), ("response_tokens", "INTEGER"),
                         ("response_tokens_est", "INTEGER"), ("entry", "TEXT")):
        if column not in columns:
            connection.execute(f"ALTER TABLE traces ADD COLUMN {column} {kind}")
    connection.execute("""CREATE TABLE IF NOT EXISTS health_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, route TEXT NOT NULL, checked_at REAL NOT NULL,
        status INTEGER, latency_ms INTEGER, ok INTEGER NOT NULL, error TEXT
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS health_checks_route ON health_checks (route, id)")
    hop_columns = {row[1] for row in connection.execute("PRAGMA table_info(hops)")}
    if hop_columns and "trace_id" not in hop_columns:
        connection.execute("DROP TABLE hops")
    connection.execute("""CREATE TABLE IF NOT EXISTS hops (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL, route TEXT NOT NULL,
        arrived_at REAL NOT NULL, ttfb_ms INTEGER, total_ms INTEGER, status INTEGER,
        classifier_us INTEGER, error TEXT
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS hops_trace ON hops (trace_id)")
    connection.execute("""CREATE TABLE IF NOT EXISTS arch_state (ts REAL PRIMARY KEY, state TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        arm TEXT NOT NULL, summary TEXT NOT NULL
    )""")
    return connection


def extract_response(path: str, headers: dict[str, str], raw: bytes) -> tuple[str | None, int | None, bool]:
    none = (None, None, False)
    if path.split("?", 1)[0] != "/v1/chat/completions" or not raw:
        return none
    lowered = {key.lower(): value for key, value in headers.items()}
    if lowered.get("content-encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            return none
    text = raw.decode("utf-8", errors="replace")
    answer: str | None = None
    tokens: int | None = None
    try:
        body = json.loads(text)
        message = body["choices"][0]["message"]
        answer = message.get("content")
        if not answer and message.get("tool_calls"):
            answer = json.dumps(message["tool_calls"])
        tokens = (body.get("usage") or {}).get("completion_tokens")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
        parts = []
        for line in text.splitlines():
            if not line.startswith("data:") or line[5:].strip() == "":
                continue
            try:
                chunk = json.loads(line[5:])
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            if isinstance(chunk.get("usage"), dict) and chunk["usage"].get("completion_tokens") is not None:
                tokens = chunk["usage"]["completion_tokens"]
            try:
                delta = chunk["choices"][0].get("delta", {})
            except (KeyError, IndexError, TypeError, AttributeError):
                continue
            if isinstance(delta.get("content"), str):
                parts.append(delta["content"])
        answer = "".join(parts)
    if not isinstance(answer, str) or not answer.strip():
        return none
    answer = answer.strip()
    estimated = not isinstance(tokens, int)
    if estimated:
        tokens = max(1, round(len(answer) / 4))
    stored = answer if len(answer) <= RESPONSE_MAX_CHARS else answer[:RESPONSE_MAX_CHARS] + "…"
    return stored, tokens, estimated


def extract_question(path: str, body: bytes) -> str | None:
    if path.split("?", 1)[0] != "/v1/chat/completions" or not body:
        return None
    try:
        messages = json.loads(body).get("messages", [])
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip() or None
        if isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n\n".join(parts) or None
    return None


def _is_sensitive_entry(entry: str | None) -> bool:
    """Both the sensitive and the sensitive-judgment entries carry PII."""
    return (entry or "").startswith("sensitive")


def create_trace(trace_id: str, method: str, path: str, request_id: str, question: str | None,
                 entry: str) -> None:
    if _is_sensitive_entry(entry) and not STORE_SENSITIVE_CONTENT:
        question = None
    with DB_LOCK, db() as connection:
        connection.execute(
            "INSERT INTO traces (id, started_at, method, path, request_id, fallback, question, entry) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (trace_id, time.time(), method, path, request_id, question, entry),
        )


def record_hop(trace_id: str, route: str, classification: str | None) -> None:
    """Attach a hop to one trace by its server-generated id.

    Never key this on a client-supplied request id: two concurrent clients
    reusing one id would have their hops written to each other's traces.
    """
    with DB_LOCK, db() as connection:
        connection.execute(
            "UPDATE traces SET classification=COALESCE(classification, ?), route=COALESCE(route, ?) "
            "WHERE id=? AND completed_at IS NULL",
            (classification, route, trace_id),
        )


def record_hop_timing(trace_id: str, route: str, arrived_at: float, started: float, ttfb_ms: int | None,
                      status: int | None, classifier_us: str | None, error: str | None = None) -> None:
    try:
        classifier = int(classifier_us) if classifier_us else None
    except ValueError:
        classifier = None
    with DB_LOCK, db() as connection:
        connection.execute(
            "INSERT INTO hops (trace_id, route, arrived_at, ttfb_ms, total_ms, status, classifier_us, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (trace_id, route, arrived_at, ttfb_ms, round((time.monotonic() - started) * 1000),
             status, classifier, error),
        )


def complete_trace(trace_id: str, status: int, headers: dict[str, str], started: float,
                   error: str | None = None,
                   response: tuple[str | None, int | None, bool] = (None, None, False)) -> None:
    classification, route, fallback = routing_signal(headers)
    response_text, response_tokens, tokens_estimated = response
    with DB_LOCK, db() as connection:
        row = connection.execute("SELECT classification, route, entry FROM traces WHERE id=?",
                                 (trace_id,)).fetchone()
        if classification is None or route is None:
            if row:
                classification = classification or row[0]
                route = route or row[1]
        if row and _is_sensitive_entry(row[2]) and not STORE_SENSITIVE_CONTENT:
            response_text = None
        # Degradation without a classifier: an errored hop on a request that
        # still completed means the failover set served it. The header never
        # worked (FINDINGS §7: praxis does not emit X-Praxis-Fallback on
        # proxied failovers) and the label/route mismatch signal left with
        # the classifier, so the failed hop is the durable signal.
        if not fallback and status == 200:
            errored = connection.execute(
                "SELECT 1 FROM hops WHERE trace_id=? AND error IS NOT NULL LIMIT 1",
                (trace_id,)).fetchone()
            if errored:
                fallback = True
        # Degradation is an implication, not an equivalence: GPU-tier work on
        # a non-GPU pool is degraded, but SIMPLE/MEDIUM work on a GPU pool
        # (arm B's intended split) is not.
        if classification and route and classification.upper() in GPU_LABELS \
                and "gpu" not in route.lower():
            fallback = True
        connection.execute("""UPDATE traces SET completed_at=?, status=?, classification=?, route=?, fallback=?,
            duration_ms=?, response_headers=?, error=?, response=?, response_tokens=?, response_tokens_est=?
            WHERE id=?""", (
            time.time(), status, classification, route, fallback,
            round((time.monotonic() - started) * 1000), json.dumps(headers), error,
            response_text, response_tokens, int(tokens_estimated), trace_id,
        ))
        connection.execute("DELETE FROM traces WHERE id NOT IN (SELECT id FROM traces ORDER BY started_at DESC LIMIT ?)", (MAX_TRACES,))
        connection.execute("DELETE FROM hops WHERE trace_id NOT IN (SELECT id FROM traces)")
        # Optional portable export alongside SQLite (TRACE_OTEL=jsonl|otlp).
        # Never let an exporter failure break the relay or lose the audit row.
        row = connection.execute(
            "SELECT id, started_at, completed_at, method, path, status, classification, "
            "route, fallback, duration_ms, response_headers, entry, question, response, "
            "response_tokens, response_tokens_est, error FROM traces WHERE id=?",
            (trace_id,),
        ).fetchone()
        if row is not None:
            columns = ("id", "started_at", "completed_at", "method", "path", "status",
                       "classification", "route", "fallback", "duration_ms",
                       "response_headers", "entry", "question", "response",
                       "response_tokens", "response_tokens_est", "error")
            hop_rows = connection.execute(
                "SELECT route, arrived_at, ttfb_ms, total_ms, status, classifier_us, error "
                "FROM hops WHERE trace_id=? ORDER BY arrived_at", (trace_id,)
            ).fetchall()
            hop_columns = ("route", "arrived_at", "ttfb_ms", "total_ms", "status",
                           "classifier_us", "error")
            try:
                otel.emit_trace(dict(zip(columns, row, strict=False)),
                                [dict(zip(hop_columns, hop, strict=False))
                                 for hop in hop_rows])
            except Exception as export_error:  # noqa: BLE001 - exporter must not break relay
                print(f"traceui: OTel export failed: {export_error}")


def routing_signal(headers: dict[str, str]) -> tuple[str | None, str | None, bool]:
    lowered = {key.lower(): value for key, value in headers.items()}
    classification = (lowered.get("x-demo-classification") or lowered.get("x-llm-d-sc-label")
                      or next((value for key, value in lowered.items()
                               if "classif" in key or key.endswith("-label")), None))
    route = (lowered.get("x-demo-route") or lowered.get("x-praxis-cluster")
             or lowered.get("x-praxis-route")
             or next((value for key, value in lowered.items()
                      if "cluster" in key or "route" in key or "upstream" in key), None))
    fallback = any("fallback" in key and value.lower() in {"true", "1", "yes"}
                   for key, value in lowered.items())
    if route is None and classification:
        route = "vllm-gpu" if classification.upper() in {"COMPLEX", "REASONING"} else "vllm-cpu"
    wants_gpu = classification is not None and classification.upper() in GPU_LABELS
    if wants_gpu and route and "gpu" not in route.lower():
        fallback = True
    return classification, route, fallback


def record_health(route: str, status: int | None, latency_ms: int, error: str | None) -> None:
    with DB_LOCK, db() as connection:
        connection.execute(
            "INSERT INTO health_checks (route, checked_at, status, latency_ms, ok, error) VALUES (?, ?, ?, ?, ?, ?)",
            (route, time.time(), status, latency_ms, int(status == 200), error),
        )
        connection.execute(
            "DELETE FROM health_checks WHERE route=? AND id NOT IN "
            "(SELECT id FROM health_checks WHERE route=? ORDER BY id DESC LIMIT ?)",
            (route, route, HEALTH_KEEP),
        )


def probe_state(route: str) -> bool | None:
    with DB_LOCK, db() as connection:
        row = connection.execute(
            "SELECT checked_at, ok FROM health_checks WHERE route=? ORDER BY id DESC LIMIT 1", (route,)
        ).fetchone()
    if row is None or time.time() - row[0] > PROBE_FRESH_SECONDS:
        return None
    return bool(row[1])


def k8s_get(path: str) -> dict:
    token = (SA_DIR / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(SA_DIR / "ca.crt"))
    request = Request(K8S_API + path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urlopen(request, timeout=4, context=context) as response:
        return json.load(response)


def k8s_patch(path: str, body: dict) -> dict:
    token = (SA_DIR / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(SA_DIR / "ca.crt"))
    request = Request(K8S_API + path, data=json.dumps(body).encode(), method="PATCH",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                               "Content-Type": "application/merge-patch+json"})
    with urlopen(request, timeout=4, context=context) as response:
        return json.load(response)


def traffic_state(desired: int | None, pods: list[dict]) -> str:
    if desired:
        return "ready" if any(p["ready"] and not p["terminating"] for p in pods) else "initializing"
    return "terminating" if pods else "stopped"


def read_traffic() -> dict:
    try:
        namespace = (SA_DIR / "namespace").read_text().strip()
        spec = k8s_get(f"/apis/apps/v1/namespaces/{namespace}/deployments/{TRAFFIC_DEPLOYMENT}")
        items = k8s_get(f"/api/v1/namespaces/{namespace}/pods?labelSelector=app%3D{TRAFFIC_LABEL}")["items"]
    except (OSError, ValueError, KeyError) as failure:
        return {"state": "unavailable", "error": str(failure)[:160]}
    pods = []
    for item in items:
        status = item.get("status", {})
        if status.get("phase") not in ("Pending", "Running"):
            continue
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
        pods.append({"ready": ready, "terminating": bool(item["metadata"].get("deletionTimestamp"))})
    desired = spec.get("spec", {}).get("replicas") or 0
    return {"state": traffic_state(desired, pods), "desired": desired, "pods": len(pods)}


def set_traffic(replicas: int) -> None:
    namespace = (SA_DIR / "namespace").read_text().strip()
    k8s_patch(f"/apis/apps/v1/namespaces/{namespace}/deployments/{TRAFFIC_DEPLOYMENT}/scale",
              {"spec": {"replicas": replicas}})


def read_pools() -> dict:
    namespace = (SA_DIR / "namespace").read_text().strip()
    pools = {}
    for route, (upstream, _port, deployment, isvc) in POOLS.items():
        # The cpu-real overlays point the upstream at a plain Deployment
        # (cpureal-vllm-*) rather than a KServe predictor. Derive the
        # deployment name from the upstream host so the dashboard follows the
        # same wiring the relay uses.
        host = urlsplit(upstream).hostname or ""
        looks_like_service = host and host != "localhost" \
            and not host.replace(".", "").isdigit()
        if looks_like_service and not host.startswith("vllm-"):
            deployment = host
        spec = k8s_get(f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}")
        items = k8s_get(
            f"/api/v1/namespaces/{namespace}/pods?labelSelector=serving.kserve.io%2Finferenceservice%3D{isvc}"
        )["items"]
        if not items:
            # Plain Deployments label their pods with app=<name>.
            items = k8s_get(
                f"/api/v1/namespaces/{namespace}/pods?labelSelector=app%3D{deployment}"
            )["items"]
        pods = []
        for item in sorted(items, key=lambda entry: entry["metadata"]["name"]):
            status = item.get("status", {})
            if status.get("phase") not in ("Pending", "Running"):
                continue
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
            pods.append({"name": item["metadata"]["name"], "ready": ready,
                         "terminating": bool(item["metadata"].get("deletionTimestamp"))})
        pools[route] = {"desired": spec.get("spec", {}).get("replicas"), "pods": pods}
    return pools


def read_state() -> dict:
    error = None
    try:
        pools = read_pools()
    except (OSError, ValueError, KeyError) as failure:
        pools, error = {route: {} for route in POOLS}, str(failure)[:160]
    for route, pool in pools.items():
        pool["probe"] = probe_state(route)
    state: dict = {"k8s": error is None, "pools": pools, "routes": list(POOLS)}
    if error:
        state["error"] = error
    return state


STATE_LOCK = threading.Lock()
LIVE_STATE: dict = {"state": None, "at": None}


def state_poller() -> None:
    last = None
    while True:
        try:
            state = read_state()
            key = json.dumps(state, sort_keys=True)
            now = time.time()
            with STATE_LOCK:
                LIVE_STATE.update(state=state, at=now)
            if key != last:
                with DB_LOCK, db() as connection:
                    connection.execute("INSERT OR REPLACE INTO arch_state (ts, state) VALUES (?, ?)", (now, key))
                    connection.execute(
                        "DELETE FROM arch_state WHERE ts NOT IN (SELECT ts FROM arch_state ORDER BY ts DESC LIMIT ?)",
                        (STATE_KEEP,),
                    )
                last = key
        except Exception as error:
            print(json.dumps({"event": "state_poll_error", "error": str(error)[:200]}), flush=True)
        time.sleep(STATE_POLL_SECONDS)


def state_payload(path: str) -> dict:
    at = parse_qs(urlsplit(path).query).get("at", [None])[0]
    with DB_LOCK, db() as connection:
        first = connection.execute("SELECT MIN(ts) FROM arch_state").fetchone()[0]
        row = None
        if at is not None:
            try:
                row = connection.execute(
                    "SELECT state FROM arch_state WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (float(at),)
                ).fetchone()
            except ValueError:
                pass
    if at is None:
        with STATE_LOCK:
            return {"now": time.time(), "state": LIVE_STATE["state"], "history_starts": first}
    return {"at": at, "state": json.loads(row[0]) if row else None, "history_starts": first}


def traces() -> list[dict]:
    with DB_LOCK, db() as connection:
        rows = connection.execute("""SELECT id, started_at, completed_at, method, path, request_id,
            status, classification, route, fallback, duration_ms, response_headers, error, question, response,
            response_tokens, response_tokens_est, entry
            FROM traces ORDER BY started_at DESC LIMIT ?""", (MAX_TRACES,)).fetchall()
    ids = [row[0] for row in rows]
    hops: dict[str, list[dict]] = {}
    if ids:
        with DB_LOCK, db() as connection:
            hop_rows = connection.execute(
                f"SELECT trace_id, route, arrived_at, ttfb_ms, total_ms, status, classifier_us, error "
                f"FROM hops WHERE trace_id IN ({','.join('?' * len(ids))}) ORDER BY arrived_at", ids).fetchall()
        started = {row[0]: row[1] for row in rows}
        for trace_key, route, arrived_at, ttfb, total, status, classifier, error in hop_rows:
            hops.setdefault(trace_key, []).append({
                "route": route, "offset_ms": round((arrived_at - started[trace_key]) * 1000),
                "ttfb_ms": ttfb, "total_ms": total, "status": status,
                "classifier_ms": round(classifier / 1000) if classifier is not None else None, "error": error})
    keys = ("id", "started_at", "completed_at", "method", "path", "request_id", "status", "classification",
            "route", "fallback", "duration_ms", "response_headers", "error", "question", "response",
            "response_tokens", "response_tokens_est", "entry")
    return [{**dict(zip(keys, row, strict=False)), "response_headers": json.loads(row[11]),
             "hops": hops.get(row[0], [])} for row in rows]


def trace_summary() -> dict:
    """Aggregate counters over stored traces: the governance check reads this.

    by_route/entry counts every completed trace; fallback and label counts come
    from the same rows the UI shows, so a claim like "zero sensitive work on a
    non-approved pool" can be verified independently of the pipeline's records.
    """
    with DB_LOCK, db() as connection:
        rows = connection.execute("""SELECT classification, route, fallback, entry, status,
            COUNT(*), AVG(duration_ms), SUM(response_tokens)
            FROM traces WHERE completed_at IS NOT NULL
            GROUP BY classification, route, fallback, entry, status""").fetchall()
        total = connection.execute("SELECT COUNT(*) FROM traces WHERE completed_at IS NOT NULL").fetchone()[0]
    groups = []
    for classification, route, fallback, entry, status, count, avg_ms, tokens in rows:
        groups.append({"classification": classification, "route": route, "fallback": bool(fallback),
                       "entry": entry, "status": status, "count": count,
                       "avg_duration_ms": round(avg_ms or 0), "tokens": tokens or 0})
    # Raw traces are a rolling window; per-run summaries in the `runs` table
    # are the durable audit record. Callers must not read `total` as the
    # whole-run step count when a run exceeded this retention.
    return {"total": total, "groups": groups, "routes": list(POOLS),
            "retention": MAX_TRACES}


def clear_traces() -> None:
    with DB_LOCK, db() as connection:
        connection.execute("DELETE FROM traces")
        connection.execute("DELETE FROM hops")


def record_run(summary: dict) -> dict:
    with DB_LOCK, db() as connection:
        connection.execute("INSERT INTO runs (ts, arm, summary) VALUES (?, ?, ?)",
                           (time.time(), str(summary.get("arm", "?")), json.dumps(summary)))
        connection.execute("DELETE FROM runs WHERE id NOT IN "
                           "(SELECT id FROM runs ORDER BY id DESC LIMIT 200)")
    return {"ok": True}


def list_runs() -> list[dict]:
    """Latest run per arm, newest first: the configs panel and report cross-check.

    Old summaries carry only ``arm``; new ones also carry the policy ``config``.
    The page prefers ``config`` and falls back to the recorded letter, so this
    file stays standalone (it is mounted as a single ConfigMap) and does not
    import the Python config catalog.
    """
    with DB_LOCK, db() as connection:
        rows = connection.execute(
            "SELECT id, ts, arm, summary FROM runs ORDER BY id DESC"
        ).fetchall()
    seen: set[str] = set()
    runs = []
    for _id, ts, arm, summary in rows:
        if arm in seen:
            continue
        try:
            data = json.loads(summary)
        except json.JSONDecodeError:
            continue
        # An MCP review or a one-document smoke is a functional check, not a
        # measured config arm. It must not displace the latest matrix row.
        if data.get("run_kind") in {"mcp", "smoke"}:
            continue
        seen.add(arm)
        data["ts"] = ts
        data["run_id"] = _id
        runs.append(data)
    return runs


def audit_jsonl() -> str:
    """One JSON object per completed trace: the take-home audit evidence."""
    lines = []
    for trace in traces():
        if trace.get("completed_at") is None:
            continue
        lines.append(json.dumps({
            "ts": trace["started_at"],
            "completed_at": trace["completed_at"],
            "request_id": trace["request_id"],
            "entry": trace.get("entry"),
            "classification": trace.get("classification"),
            "route": trace.get("route"),
            "fallback": trace.get("fallback"),
            "status": trace.get("status"),
            "duration_ms": trace.get("duration_ms"),
            "response_tokens": trace.get("response_tokens"),
            "hops": trace.get("hops"),
        }, ensure_ascii=False))
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PraxisTraceRelay/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _needs_auth(self) -> bool:
        path = self.path.split("?", 1)[0]
        return (self.server.route is None and API_TOKEN
                and path != "/_trace/healthz" and not self.authorized())

    def do_GET(self) -> None:
        if self._needs_auth():
            self.json(401, {"error": "bearer token required"})
            return
        if self.server.route:
            self.proxy()
        elif self.path in {"/", "/index.html", "/_trace", "/_trace/", "/_trace/index.html"}:
            self.html(200, PAGE)
        elif self.path == "/_trace/healthz":
            self.json(200, {"status": "ok"})
        elif self.path == "/_trace/api/traces":
            self.json(200, {"traces": traces(), "upstream": UPSTREAM, "retention": MAX_TRACES})
        elif self.path == "/_trace/api/summary":
            self.json(200, trace_summary())
        elif self.path == "/_trace/api/runs":
            self.json(200, {"runs": list_runs()})
        elif self.path == "/_trace/api/audit.jsonl":
            raw = audit_jsonl().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Disposition", 'attachment; filename="audit.jsonl"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path.split("?", 1)[0] == "/_trace/api/state":
            self.json(200, state_payload(self.path))
        elif self.path == "/_trace/api/traffic":
            self.json(200, read_traffic())
        else:
            self.proxy()

    def do_POST(self) -> None:
        if self._needs_auth():
            self.json(401, {"error": "bearer token required"})
            return
        if self.server.route is None and self.path == "/_trace/api/traffic":
            self.traffic_action()
        elif self.server.route is None and self.path == "/_trace/api/runs":
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                summary = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self.json(400, {"error": "body must be JSON"})
                return
            self.json(200, record_run(summary))
        else:
            self.proxy()

    def traffic_action(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            action = json.loads(raw or b"{}").get("action")
        except (ValueError, AttributeError):
            action = None
        if action not in ("start", "stop"):
            self.json(400, {"error": "action must be start or stop"})
            return
        try:
            set_traffic(1 if action == "start" else 0)
        except (OSError, ValueError) as failure:
            self.json(502, {"state": "unavailable", "error": str(failure)[:160]})
            return
        self.json(200, read_traffic())

    def _write_allowed(self) -> bool:
        # Only relay OpenAI-compatible API methods; arbitrary paths would
        # turn the public trace UI into an open proxy.
        return self.path.split("?", 1)[0].startswith("/v1/")

    def _reject_write(self) -> None:
        self.json(405, {"error": "method not allowed on this path"})

    def do_PUT(self) -> None:
        if self._write_allowed():
            self.proxy()
        else:
            self._reject_write()

    def do_PATCH(self) -> None:
        if self._write_allowed():
            self.proxy()
        else:
            self._reject_write()

    def do_DELETE(self) -> None:
        if self._needs_auth():
            self.json(401, {"error": "bearer token required"})
        elif self.path == "/_trace/api/traces":
            clear_traces()
            self.json(200, {"cleared": True})
        elif self._write_allowed():
            self.proxy()
        else:
            self._reject_write()

    def do_OPTIONS(self) -> None:
        if self._write_allowed():
            self.proxy()
        else:
            self._reject_write()

    def json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def html(self, status: int, body: str) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self) -> bool:
        if not API_TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {API_TOKEN}"

    def proxy(self) -> None:
        route = self.server.route
        upstream = self.server.upstream
        entry = self.server.entry
        trace_id, started = str(uuid.uuid4()), time.monotonic()
        client_request_id = self.headers.get("X-Request-Id")
        # Correlate on the server-generated id; the client id is forwarded
        # upstream but never keys the trace (two clients can reuse one).
        request_id = trace_id
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if route is None:
            create_trace(trace_id, self.command, self.path, request_id, extract_question(self.path, body), entry)
        # Hops arrive with the ingress trace id in the _trace query parameter;
        # that id, not this relay's own uuid, keys the trace row.
        tagged = dict(parse_qsl(urlsplit(self.path).query)).get("_trace")
        hop_trace_id = tagged or trace_id
        is_probe = route is not None and self.command == "GET" and self.path.split("?", 1)[0] == HEALTH_PATH
        if route is not None and not is_probe:
            record_hop(hop_trace_id, route, self.headers.get("X-Llm-D-Sc-Label"))
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in HOP_BY_HOP and not key.lower().startswith("x-praxis-")}
        headers["X-Request-Id"] = client_request_id or trace_id
        timed = route is not None and not is_probe
        path = self.path
        if route is None and self.command == "POST":
            path += ("&" if "?" in path else "?") + "_trace=" + trace_id
        elif route is not None:
            parts = urlsplit(path)
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "_trace"]
            path = parts.path + ("?" + urlencode(query) if query else "")
        arrived_at = time.time()
        classifier_us = self.headers.get("X-Llm-D-Sc-Latency-Us")
        upstream_request = Request(upstream + path, data=body if body else None, headers=headers, method=self.command)
        response = None
        try:
            response = urlopen(upstream_request, timeout=5 if is_probe else 600)
        except HTTPError as error:
            response = error
        except (URLError, TimeoutError, OSError) as error:
            if is_probe:
                record_health(route, None, round((time.monotonic() - started) * 1000), str(error))
            if timed:
                record_hop_timing(hop_trace_id, route, arrived_at, started, None, None, classifier_us, str(error))
            if route is None:
                complete_trace(trace_id, 502, {}, started, str(error))
            self.json(502, {"error": {"message": "Upstream unavailable", "type": "upstream_error"}})
            return
        ttfb_ms = round((time.monotonic() - started) * 1000)
        response_headers = dict(response.headers.items())
        if is_probe:
            record_health(route, response.status, round((time.monotonic() - started) * 1000), None)
        if route is not None:
            response_headers["X-Demo-Route"] = route
            classification = self.headers.get("X-Llm-D-Sc-Label")
            if classification:
                response_headers["X-Demo-Classification"] = classification
        self.send_response(response.status)
        for key, value in response_headers.items():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        if route is None:
            self.send_header("X-Praxis-Trace-Id", trace_id)
            self.send_header("X-Demo-Entry", entry)
        self.send_header("Connection", "close")
        self.end_headers()
        captured = bytearray()
        try:
            while chunk := response.read(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
                if route is None and len(captured) < RESPONSE_CAPTURE_BYTES:
                    captured += chunk[:RESPONSE_CAPTURE_BYTES - len(captured)]
            if route is None:
                complete_trace(trace_id, response.status, response_headers, started,
                               response=extract_response(self.path, response_headers, bytes(captured)))
        except (BrokenPipeError, ConnectionResetError, OSError) as error:
            if route is None:
                complete_trace(trace_id, response.status, response_headers, started, str(error),
                               response=extract_response(self.path, response_headers, bytes(captured)))
        finally:
            if timed:
                record_hop_timing(hop_trace_id, route, arrived_at, started, ttfb_ms, response.status, classifier_us)


class RelayServer(ThreadingHTTPServer):
    # The pipeline fans out concurrent documents; the socketserver default
    # backlog of 5 makes macOS refuse the overflow at connect time.
    request_queue_size = 128
    daemon_threads = True

    def __init__(self, address: tuple[str, int], upstream: str, route: str | None = None,
                 entry: str = "general"):
        super().__init__(address, Handler)
        self.upstream = upstream
        self.route = route
        self.entry = entry


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hybrid routing request traces</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--panel:#0d1b2d;--line:#2b4865;--text:#e9f2ff;--muted:#9db1c8;--blue:#64b5ff;--cyan:#55e6d0;--violet:#ad9cff;--amber:#ffc76a;--active:#ff4d5e}*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:24px;color:var(--text);background:radial-gradient(circle at 15% 5%,#17385a 0,transparent 30%),radial-gradient(circle at 90% 95%,#173c3b 0,transparent 28%),var(--bg);font:15px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}main{width:min(1040px,100%);margin:auto}header,.section-heading,.drawer-header{display:flex;align-items:center;justify-content:space-between;gap:16px}header{margin-bottom:22px}h1{margin:0;font-size:25px}h2{margin:0}.section-heading{margin-bottom:12px}header p{margin:5px 0 0;color:var(--muted)}button{border:1px solid #365775;border-radius:10px;padding:7px 12px;color:var(--text);background:#172b41;font:inherit;cursor:pointer}button:hover{border-color:var(--blue);background:#1c3855}button:disabled{cursor:wait;opacity:.6}.live{color:var(--cyan);font-weight:700}.live:before{content:'●';margin-right:7px}.graphic,.traces{overflow:hidden;border:1px solid #23415d;border-radius:24px;background:linear-gradient(145deg,rgba(13,27,45,.97),rgba(7,17,31,.97));box-shadow:0 24px 80px #00000059}.canvas{padding:26px 30px 18px;overflow-x:auto}svg{display:block;width:100%;height:auto}.label{fill:var(--muted);font-size:12px;font-weight:700;letter-spacing:.07em}.small{fill:var(--muted);font-size:12px}.title{fill:var(--text);font-size:18px;font-weight:700}.box{fill:var(--panel);stroke:var(--line);stroke-width:1.5}.box-blue{fill:#102d49;stroke:var(--blue)}.box-cyan{fill:#103b3c;stroke:var(--cyan)}.box-violet{fill:#28234b;stroke:var(--violet)}.box-amber{fill:#45331a;stroke:var(--amber)}.box-green{fill:#123b2a;stroke:#55e6a0}.pill{fill:#172b41;stroke:#365775;stroke-width:1}.pill-text{fill:#c5d7ea;font-size:11px;font-weight:600}.flow{fill:none;stroke:var(--blue);stroke-width:2.5;marker-end:url(#arrow-blue)}.flow-cyan{fill:none;stroke:var(--cyan);stroke-width:2.5;marker-end:url(#arrow-cyan)}.cpu-path{stroke:var(--violet)}.gpu-path{stroke:var(--amber)}
.cluster{fill:#0b1830;stroke:#1e3a55;stroke-width:1.5}.cluster-label{fill:var(--muted);font-size:12px;font-weight:700;letter-spacing:.07em}
.box-grey{fill:#131c2a;stroke:#3a4b61;stroke-width:1.5}.dashed{stroke-dasharray:6 5}.pod-title{fill:var(--text);font-size:12px;font-weight:700}.pod-title.dim{fill:#8da0b8}.pod-sub{fill:var(--muted);font-size:11px}#state-note{fill:var(--amber);font-size:12px;font-weight:700}
.tl{display:flex;height:10px;border-radius:4px;overflow:hidden;margin:6px 0;background:#13243a}.tl i{min-width:2px}.sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px}.k-classify{background:var(--violet)}.k-praxis{background:var(--blue)}.k-backend{background:var(--cyan)}.k-fail{background:var(--active)}.k-gap{background:var(--amber)}.tl-line{display:block;color:var(--muted)}.tl-line b{color:var(--text)}
.actions{display:flex;align-items:center;gap:10px}.traffic-status{display:inline-flex;align-items:center;gap:7px;color:var(--muted);font-size:13px}.traffic-status i{width:9px;height:9px;border-radius:50%;background:#6b7c90}.traffic-status.ready i{background:#55e6a0}.traffic-status.initializing i,.traffic-status.terminating i{background:var(--amber)}.traffic-status.unavailable i{background:var(--active)}
.small-link{color:var(--cyan);font-size:13px;text-decoration:none}
.small-link:hover{text-decoration:underline}
.arm-row.bad{color:var(--active)}
.entry-badge{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:6px;font-size:10px;font-weight:700;letter-spacing:.05em;background:#28234b;color:#c9b8ff;border:1px solid #4b3f86}
.drawer-backdrop{position:fixed;inset:0;background:#02081099;opacity:0;pointer-events:none;transition:opacity .18s;z-index:40}
.drawer-backdrop.open{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(480px,94vw);background:var(--panel);border-left:1px solid var(--line);box-shadow:-28px 0 90px #00000082;padding:22px 26px;overflow-y:auto;transform:translateX(102%);transition:transform .22s ease;z-index:50}
.drawer.open{transform:none}
.drawer-close{flex:none;line-height:1;padding:4px 12px;font-size:18px}
.drawer-meta{color:var(--muted);font-size:12.5px;margin:2px 0 14px}
.question-text{white-space:pre-wrap;word-break:break-word;font:13.5px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#0a1626;border:1px solid var(--line);border-radius:12px;padding:14px;max-height:65vh;overflow-y:auto}
/* The packet paths only exist so the request dot can be animated along them
   (getPointAtLength); they must not paint. Without this they inherit SVG's
   default fill:black and render as black wedges under the routing arrows. */
.packet-path{fill:none;stroke:none}
.flow.selected{opacity:1;stroke-width:4.5}
.approved-path{stroke:#55e6a0;stroke-dasharray:6 5;marker-end:url(#arrow-green)}
.small-path{stroke:var(--cyan);stroke-dasharray:6 5;marker-end:url(#arrow-cyan)}
.route-note{fill:var(--muted);font-size:11px}
.entry-pill{font-size:10px}
.legend{display:flex;flex-wrap:wrap;gap:8px 22px;padding:2px 30px 18px;color:var(--muted);font-size:12px}
.legend span{display:inline-flex;align-items:center;gap:8px}
.swatch{display:inline-block;width:22px;height:3px;border-radius:2px;background:var(--blue)}
.swatch.cyan{background:var(--cyan)}
.swatch.dashed{height:0;background:none;border-top:3px dashed #55e6a0}
</style>
</head>
<body><main>
<header><div><h1>Hybrid routing request traces</h1><p>Live routing across the CPU, GPU, approved-GPU and small-GPU pools; sensitive, judgment and sensitive-judgment entries are badged.</p></div><span class="live">LIVE</span></header>
<section class="graphic"><div class="canvas"><svg viewBox="0 0 980 660" role="img" aria-labelledby="diagram-title diagram-desc">
<title id="diagram-title">Live Praxis request routing</title><desc id="diagram-desc">The contract pipeline sends each step to one of four Praxis entries. Each entry pins its requests to a pool and, where configured, a failover set. Four pools serve the routes: CPU, GPU, approved-GPU and small-GPU.</desc>
<defs><marker id="arrow-blue" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L10,3 L0,6 z" fill="#64b5ff"/></marker><marker id="arrow-cyan" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L10,3 L0,6 z" fill="#55e6d0"/></marker><marker id="arrow-red" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L10,3 L0,6 z" fill="#ff4d5e"/></marker><marker id="arrow-green" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L10,3 L0,6 z" fill="#55e6a0"/></marker><marker id="arrow-violet" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L10,3 L0,6 z" fill="#ad9cff"/></marker><marker id="arrow-amber" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L10,3 L0,6 z" fill="#ffc76a"/></marker></defs>
<path id="packet-source" class="packet-path" d="M490 114 V150"/><path id="packet-classify" class="packet-path" d="M694 190 H724"/><path id="packet-result" class="packet-path" d="M724 214 H694"/><path id="packet-cpu" class="packet-path" d="M400 280 V306 H257 V330"/><path id="packet-gpu" class="packet-path" d="M580 280 V306 H722 V330"/><path id="packet-approved" class="packet-path" d="M490 280 V486 H257 V510"/><path id="packet-small" class="packet-path" d="M490 280 V486 H722 V510"/><circle id="request-dot" class="packet" r="7" fill="#64b5ff"/>

<text x="0" y="26" class="label">REQUEST</text><rect x="290" y="38" width="400" height="76" rx="18" class="box-blue"/><text x="330" y="72" class="title">Client / pipeline</text><text x="330" y="93" class="small">classify · extract · risk · report — one call per step</text><path id="flow-source" d="M490 114 V150" class="flow"/>

<text x="0" y="140" class="label">ROUTING POLICY</text><rect x="286" y="150" width="408" height="130" rx="18" class="box-blue"/><text x="312" y="184" class="title">Praxis / AI Gateway</text><text x="312" y="206" class="small">Applies policy, then proxies to the entry pin</text>
<rect x="296" y="222" width="94" height="24" rx="12" class="pill"/><text x="343" y="238" text-anchor="middle" class="pill-text entry-pill">general :8080</text><rect x="394" y="222" width="94" height="24" rx="12" class="pill"/><text x="441" y="238" text-anchor="middle" class="pill-text entry-pill">sensitive :8083</text><rect x="492" y="222" width="94" height="24" rx="12" class="pill"/><text x="539" y="238" text-anchor="middle" class="pill-text entry-pill">judgment :8086</text><rect x="590" y="222" width="94" height="24" rx="12" class="pill"/><text x="637" y="238" text-anchor="middle" class="pill-text entry-pill">sens-judge :8087</text>
<rect x="724" y="150" width="226" height="130" rx="18" class="box-cyan"/><text x="750" y="184" class="title">Policy configs</text><text x="750" y="206" class="small">Pinned, hot-swapped</text><rect x="750" y="222" width="86" height="24" rx="12" class="pill"/><text x="793" y="238" text-anchor="middle" class="pill-text">hybrid</text><rect x="844" y="222" width="86" height="24" rx="12" class="pill"/><text x="887" y="238" text-anchor="middle" class="pill-text">all-gpu</text><path id="flow-classify" d="M694 190 H724" class="flow-cyan"/><path id="flow-result" d="M724 214 H694" class="flow-cyan"/>

<text x="0" y="308" class="label">WORKLOADS — ONE POOL PER ROUTE</text><text id="state-note" x="490" y="12" text-anchor="middle"></text>
<path id="flow-cpu" d="M400 280 V306 H257 V330" class="flow cpu-path"/><path id="flow-gpu" d="M580 280 V306 H722 V330" class="flow gpu-path"/><path id="flow-approved" d="M490 280 V486 H257 V510" class="flow approved-path"/><path id="flow-small" d="M490 280 V486 H722 V510" class="flow small-path"/>
<text x="270" y="325" class="route-note">general + sensitive · CPU-first pin</text><text x="710" y="325" text-anchor="end" class="route-note">judgment · risk + report</text><text x="270" y="505" class="route-note">sensitive failover · sens-judge pin</text><text x="710" y="505" text-anchor="end" class="route-note">arm B only · idle in hybrid</text>

<rect x="55" y="330" width="405" height="120" rx="20" class="cluster"/><text x="82" y="362" class="cluster-label">CPU pool</text><text id="pool-vllm-cpu-count" x="440" y="362" text-anchor="end" class="small"></text><g id="pods-vllm-cpu"></g><rect x="520" y="330" width="405" height="120" rx="20" class="cluster"/><text x="547" y="362" class="cluster-label">GPU pool</text><text id="pool-vllm-gpu-count" x="905" y="362" text-anchor="end" class="small"></text><g id="pods-vllm-gpu"></g>
<g id="pools-extra"></g>
</svg></div><footer class="legend"><span><i class="swatch"></i> request / routing path</span><span><i class="swatch cyan"></i> policy lookup</span><span><i class="swatch dashed"></i> failover / arm-specific</span></footer></section>

<section class="traces"><div class="section-heading"><h2>Recent requests <small id="count"></small></h2><div class="actions"><span id="traffic-status" class="traffic-status unknown"><i></i>Traffic: …</span><button id="traffic-toggle" type="button" disabled>Simulate Traffic</button><button id="clear-recents" type="button">Clear Recents</button></div></div><table><thead><tr><th>Time</th><th>Request</th><th>Legacy label</th><th>Destination</th><th>Status</th><th>Duration</th><th>Response</th></tr></thead><tbody id="traces"><tr><td colspan="6">Waiting for a request…</td></tr></tbody></table></section>

<section class="traces"><div class="section-heading"><h2>Routing configs <small id="arms-note"></small></h2><a class="small-link" href="/_trace/api/audit.jsonl">audit.jsonl</a></div><table><thead><tr><th>Config</th><th>Mode</th><th>Docs</th><th>Wall s</th><th>CPU-served</th><th title="Measured device time per document; a shared physical backend is counted once. * = route name but CPU-backed pool.">GPU s/doc</th><th>Fallback</th><th>Errors</th><th>Sensitive violations</th></tr></thead><tbody id="arms"><tr><td colspan="9">No runs posted yet (run scripts/cluster/arms.sh or scripts/linux/arms.sh).</td></tr></tbody></table><p class="small" style="margin:8px 0 0;color:var(--muted)">GPU s/doc is measured device time, not a route-name sum; <b>*</b> means the route is CPU-backed in the recorded pool map. MCP and one-document smoke runs are excluded from this table.</p></section>
</main><div id="drawer-backdrop" class="drawer-backdrop"></div><aside id="question-drawer" class="drawer" aria-hidden="true" aria-labelledby="question-title"><div class="drawer-header"><h2 id="question-title">Question</h2><button id="drawer-close" type="button" class="drawer-close" aria-label="Close question">×</button></div><div id="question-meta" class="drawer-meta"></div><p id="question-text" class="question-text"></p></aside><script>
const $=id=>document.getElementById(id),dot=$('request-dot'),paths={source:$('packet-source'),classify:$('packet-classify'),result:$('packet-result'),cpu:$('packet-cpu'),gpu:$('packet-gpu'),approved:$('packet-approved'),small:$('packet-small')},flows={source:[$('flow-source')],classify:[$('flow-classify'),$('flow-result')],cpu:[$('flow-cpu')],gpu:[$('flow-gpu')],approved:[$('flow-approved')],small:[$('flow-small')]};let last='',lastAnimated='',selectedId='',dismissedThroughId='',traceItems=[],animating=false,animationGeneration=0,openTraces=new Set();
const questionDrawer=$('question-drawer'),drawerBackdrop=$('drawer-backdrop');
function esc(x){return String(x??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function isGpu(t){return t.route?t.route.toLowerCase().includes('gpu'):['COMPLEX','REASONING'].includes((t.classification||'').toUpperCase())}
const POOL_LABEL={'vllm-cpu':'CPU pool','vllm-gpu':'GPU pool','vllm-gpu-approved':'approved-GPU pool','vllm-gpu-small':'small-GPU pool'},ROUTE_COLOR={cpu:'#ad9cff',gpu:'#ffc76a',approved:'#55e6a0',small:'#55e6d0'};
function flowKey(t){const r=(t.route||'').toLowerCase();return r.includes('approved')?'approved':r.includes('small')?'small':isGpu(t)?'gpu':'cpu'}
function timelineHtml(t){const hops=t.hops||[];if(!hops.length||!t.duration_ms)return'';const segs=[];let cursor=0;hops.forEach((h,i)=>{const gap=Math.max(0,h.offset_ms-cursor);if(i===0){const c=h.classifier_ms!=null?Math.min(h.classifier_ms,gap):0;if(c)segs.push({k:'classify',l:'classifier (legacy)',ms:c});segs.push({k:'praxis',l:'Praxis',ms:gap-c})}else segs.push({k:'gap',l:'failover gap',ms:gap});const bad=h.status==null||h.status>=500||h.status===429;segs.push({k:bad?'fail':'backend',l:h.route+(bad?' (failed)':''),ms:h.total_ms||0,note:h.ttfb_ms!=null?'first byte '+h.ttfb_ms+' ms':(h.error||'no response')});cursor=h.offset_ms+(h.total_ms||0)});segs.push({k:'praxis',l:'Praxis return',ms:Math.max(0,t.duration_ms-cursor)});return`<div class="tl">${segs.map(x=>`<i class="k-${x.k}" style="flex:${Math.max(x.ms,0)}" title="${esc(x.l)} ${x.ms} ms"></i>`).join('')}</div>${segs.map(x=>`<span class="tl-line"><i class="sw k-${x.k}"></i>${esc(x.l)} <b>${x.ms} ms</b>${x.note?' · '+esc(x.note):''}</span>`).join('')}`}
function entryNote(e){return e==='sensitive-judgment'?' (sensitive judgment entry)':e==='judgment'?' (judgment entry)':e==='sensitive'?' (sensitive entry)':''}
function pathText(t){if(!(t.route||t.classification))return'Client → Praxis (routing metadata unavailable)';return`Client → Praxis${entryNote(t.entry)} → ${POOL_LABEL[t.route]||esc(t.route||'pool')}`}
function highlight(t){Object.values(flows).flat().forEach(path=>path.classList.remove('selected'));if(!t)return;flows.source.forEach(path=>path.classList.add('selected'));if(t.route||t.classification){flows.classify.forEach(path=>path.classList.add('selected'));flows[flowKey(t)].forEach(path=>path.classList.add('selected'))}}
async function travel(path,color,duration){const length=path.getTotalLength(),started=performance.now();dot.style.display='block';dot.setAttribute('fill',color);dot.style.color=color;return new Promise(resolve=>{function frame(now){const progress=Math.min((now-started)/duration,1),point=path.getPointAtLength(length*progress);dot.setAttribute('cx',point.x);dot.setAttribute('cy',point.y);if(progress<1)requestAnimationFrame(frame);else resolve()}requestAnimationFrame(frame)})}
async function animate(t){if(!t||animating||matchMedia('(prefers-reduced-motion: reduce)').matches)return;const generation=animationGeneration;animating=true;await travel(paths.source,'#64b5ff',450);if(generation!==animationGeneration){animating=false;return}if(t.route||t.classification){await travel(paths.classify,'#55e6d0',450);if(generation!==animationGeneration){animating=false;return}await travel(paths.result,'#55e6d0',450);if(generation!==animationGeneration){animating=false;return}const key=flowKey(t);await travel(paths[key],ROUTE_COLOR[key],900)}await sleep(300);dot.style.display='none';animating=false}
function entryBadge(t){if(t.entry==='sensitive-judgment')return'<span class="entry-badge" title="judgment step through the pinned sensitive judgment chain (GPU only, no CPU failover)">SENS-JUDGE</span>';if(t.entry==='judgment')return'<span class="entry-badge" title="judgment step through the pinned GPU judgment chain (no CPU failover)">JUDGE</span>';return t.entry==='sensitive'?'<span class="entry-badge" title="sent through the sensitive entry (approved pools only)">SENS</span>':''}
function render(){const items=traceItems;$('count').textContent=`(${items.length} saved)`;$('traces').innerHTML=items.length?items.map(t=>`<tr data-trace-id="${esc(t.id)}" tabindex="0" class="${t.id===selectedId?'selected-row':''}"><td>${new Date(t.started_at*1000).toLocaleTimeString()}</td><td><span class="tag">${esc(t.method)}</span> ${esc(t.path)}${t.question?`<button class="question-button" type="button" data-question-id="${esc(t.id)}">View question</button>`:'<span class="no-question">question unavailable</span>'}<details${openTraces.has(t.id)?' data-r="1" open':''}><summary>trace ${esc(t.request_id.slice(0,8))}</summary><div class="trace-detail"><span><b>Path:</b> ${esc(pathText(t))}</span><span><b>Request ID:</b> ${esc(t.request_id)}</span><span><b>Legacy label (classifier):</b> ${esc(t.classification||'—')}</span><span><b>Destination:</b> ${esc(t.route||'not emitted')}</span><span><b>Entry:</b> ${esc(t.entry||'general')}</span><span><b>Result:</b> ${esc(t.status||'in flight')} · ${t.duration_ms?esc(t.duration_ms+' ms'):'in progress'}${t.fallback?' · fallback':''}</span>${timelineHtml(t)}${t.error?`<span><b>Error:</b> ${esc(t.error)}</span>`:''}</div></details></td><td>${esc(t.classification||'—')}${entryBadge(t)}</td><td class="${t.route?(isGpu(t)?'gpu':'cpu'):'unknown'}">${esc(t.route||'unavailable')}${t.fallback?'<small class="fellback">fell back</small>':''}</td><td>${esc(t.status||'in flight')}</td><td>${t.duration_ms?esc(t.duration_ms+' ms'):'—'}</td><td>${t.response?`<button class="question-button" type="button" data-response-id="${esc(t.id)}">${t.response_tokens?(t.response_tokens_est?'~':'')+esc(t.response_tokens)+' tokens':'View'}</button>`:'<span class="no-question">—</span>'}</td></tr>`).join(''):'<tr><td colspan="7">Waiting for a request…</td></tr>'}
function syncSelection(){document.querySelectorAll('tr[data-trace-id]').forEach(row=>row.dataset.traceId&&row.classList.toggle('selected-row',row.dataset.traceId===selectedId))}
function clearSelection(){selectedId='';dismissedThroughId=(traceItems.find(t=>t.status&&(t.route||t.classification))||{}).id||'';animationGeneration++;dot.style.display='none';highlight(null);syncSelection();unpinState()}
function selectTrace(id){if(selectedId===id){clearSelection();return}const trace=traceItems.find(item=>item.id===id);if(!trace)return;selectedId=id;dismissedThroughId='';syncSelection();highlight(trace);pinState(trace);animate(trace)}
function openQuestion(id,field='question',title='Question'){const trace=traceItems.find(item=>item.id===id);if(!trace||!trace[field])return;selectedId=id;dismissedThroughId='';syncSelection();highlight(trace);pinState(trace);$('question-title').textContent=title;$('question-meta').textContent=`${new Date(trace.started_at*1000).toLocaleString()} · trace ${trace.request_id.slice(0,8)}`;$('question-text').textContent=trace[field];questionDrawer.classList.add('open');drawerBackdrop.classList.add('open');questionDrawer.setAttribute('aria-hidden','false');$('drawer-close').focus()}
function closeQuestion(){questionDrawer.classList.remove('open');drawerBackdrop.classList.remove('open');questionDrawer.setAttribute('aria-hidden','true')}
async function load(){try{const data=await fetch('/_trace/api/traces',{cache:'no-store'}).then(r=>r.json()),items=data.traces;traceItems=items;const key=items.map(x=>x.id+':'+x.status+':'+(x.classification||'')+':'+(x.response?1:0)).join();if(key!==last){last=key;render()}if(selectedId){const selected=items.find(t=>t.id===selectedId);if(selected)highlight(selected);else clearSelection()}else{const routed=items.find(t=>t.status&&(t.route||t.classification));if(routed&&routed.id!==dismissedThroughId){highlight(routed);if(routed.id!==lastAnimated){lastAnimated=routed.id;animate(routed)}}}}catch(e){$('traces').innerHTML='<tr><td colspan="7">Trace API unavailable.</td></tr>'}}
$('traces').addEventListener('click',event=>{const question=event.target.closest('[data-question-id]');if(question){openQuestion(question.dataset.questionId);return}const answer=event.target.closest('[data-response-id]');if(answer){openQuestion(answer.dataset.responseId,'response','Response');return}if(event.target.closest('details'))return;const row=event.target.closest('tr[data-trace-id]');if(row)selectTrace(row.dataset.traceId)});
$('traces').addEventListener('toggle',event=>{const d=event.target;if(d.tagName!=='DETAILS')return;const row=d.closest('tr[data-trace-id]');if(!row)return;if(d.open)openTraces.add(row.dataset.traceId);else openTraces.delete(row.dataset.traceId);if(d.dataset.r){delete d.dataset.r;return}if(d.open&&row.dataset.traceId!==selectedId)selectTrace(row.dataset.traceId)},true);
$('traces').addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){const row=event.target.closest('tr[data-trace-id]');if(row){event.preventDefault();selectTrace(row.dataset.traceId)}}});
document.addEventListener('click',event=>{if(!selectedId)return;if(event.target.closest('tr[data-trace-id]'))return;if(questionDrawer.classList.contains('open')&&event.target.closest('#question-drawer'))return;clearSelection()});
$('drawer-close').addEventListener('click',closeQuestion);drawerBackdrop.addEventListener('click',closeQuestion);document.addEventListener('keydown',event=>{if(event.key==='Escape'&&questionDrawer.classList.contains('open'))closeQuestion()});
$('clear-recents').addEventListener('click',async()=>{const button=$('clear-recents');button.disabled=true;try{const response=await fetch('/_trace/api/traces',{method:'DELETE'});if(!response.ok)throw new Error();traceItems=[];selectedId='';dismissedThroughId='';last='';lastAnimated='';animationGeneration++;dot.style.display='none';highlight(null);closeQuestion();render()}finally{button.disabled=false}});
let traffic={state:'unknown'};
function renderTraffic(){const s=traffic.state,names={stopped:'Stopped',initializing:'Initializing',ready:'Ready',terminating:'Terminating',unavailable:'Unavailable'},el=$('traffic-status'),b=$('traffic-toggle'),running=s==='initializing'||s==='ready';el.className='traffic-status '+s;el.title=traffic.error||'';el.innerHTML='<i></i>Traffic: '+esc(names[s]||'…');b.textContent=running?'Stop Traffic':'Simulate Traffic';b.disabled=!(s==='stopped'||running)}
async function loadTraffic(){try{traffic=await fetch('/_trace/api/traffic',{cache:'no-store'}).then(r=>r.json())}catch(e){traffic={state:'unavailable',error:'trace API unavailable'}}renderTraffic()}
$('traffic-toggle').addEventListener('click',async()=>{const b=$('traffic-toggle'),running=traffic.state==='initializing'||traffic.state==='ready';b.disabled=true;try{traffic=await fetch('/_trace/api/traffic',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:running?'stop':'start'})}).then(r=>r.json())}catch(e){traffic={state:'unavailable',error:'trace API unavailable'}}renderTraffic()});
const POOLS={'vllm-cpu':{route:'vllm-cpu',x:80,cls:'box-violet',name:'vllm-cpu'},'vllm-gpu':{route:'vllm-gpu',x:545,cls:'box-amber',name:'vllm-gpu'},'vllm-gpu-approved':{route:'vllm-gpu-approved',x:80,cls:'box-green',name:'vllm-gpu-approved'},'vllm-gpu-small':{route:'vllm-gpu-small',x:545,cls:'box-cyan',name:'vllm-gpu-small'}};
let liveState=null,pinned=false,pinSeq=0;
function podList(cfg,state){const pool=state&&state.pools?state.pools[cfg.route]:null;
if(!pool)return{pods:[{kind:'unknown',title:cfg.name,sub:'state unknown'}],count:''};
const probe=pool.probe;
if(!state.k8s){const kind=probe===false?'down':probe===true?'up':'unknown';return{pods:[{kind,title:cfg.name,sub:probe===false?'health check failing':probe===true?'healthy':'status unknown'}],count:''}}
const list=pool.pods||[],desired=pool.desired==null?list.length:pool.desired,n=Math.max(list.length,desired),pods=[];let ready=0;
if(n===0)pods.push({kind:'zero',title:cfg.name,sub:'scaled to 0 replicas'});
for(let i=0;i<n;i++){const p=list[i];
if(!p){pods.push({kind:'pending',title:cfg.name,sub:'starting…'});continue}
const suffix=p.name.startsWith(cfg.name+'-')?p.name.slice(cfg.name.length+1):p.name,up=p.ready&&!p.terminating&&probe!==false;if(up)ready++;
const sub=p.terminating?'terminating':!p.ready?'not ready':probe===false?'health check failing':'healthy';
pods.push({kind:up?'up':(p.terminating?'pending':'down'),title:cfg.name,sub:suffix+' · '+sub})}
return{pods,count:ready+'/'+desired+' healthy'}}
function ensureExtraPool(key){if($('pool-'+key+'-count'))return;const cfg=POOLS[key],row=$('pools-extra');const g=document.createElementNS('http://www.w3.org/2000/svg','g');
const bx=cfg.x-25;
g.innerHTML=`<rect x="${bx}" y="510" width="405" height="120" rx="20" class="cluster"/><text x="${bx+27}" y="542" class="cluster-label">${esc(key)} pool</text><text id="pool-${key}-count" x="${bx+385}" y="542" text-anchor="end" class="small"></text><g id="pods-${key}"></g>`;row.appendChild(g)}
function renderPool(key,state,y){const cfg=POOLS[key];if(!cfg)return;const{pods,count}=podList(cfg,state),W=355,gap=10,shown=pods.slice(0,3),more=pods.length-shown.length,w=(W-gap*(shown.length-1))/shown.length;
const el=$('pool-'+key+'-count');if(!el)return;el.textContent=count;
$('pods-'+key).innerHTML=shown.map((p,i)=>{const x=cfg.x+i*(w+gap),up=p.kind==='up',dot=up?'#55e6a0':p.kind==='down'?'#ff4d5e':p.kind==='pending'?'#ffc76a':'#6b7c90',dashed=['zero','pending','unknown'].includes(p.kind)?' dashed':'';
return`<g><rect x="${x}" y="${y}" width="${w}" height="50" rx="12" class="${up?cfg.cls:'box-grey'}${dashed}"/><circle cx="${x+16}" cy="${y+25}" r="5" fill="${dot}"/><text x="${x+30}" y="${y+21}" class="pod-title${up?'':' dim'}">${w>=170?esc(p.title):'pod'}</text><text x="${x+30}" y="${y+39}" class="pod-sub">${esc(p.sub)}</text></g>`}).join('')+(more>0?`<text x="${cfg.x+W}" y="${y+64}" text-anchor="end" class="small">+${more} more</text>`:'')}
function renderState(state,note){renderPool('vllm-cpu',state,378);renderPool('vllm-gpu',state,378);const routes=state&&state.routes?state.routes:Object.keys(POOLS);for(const key of routes){if(key==='vllm-cpu'||key==='vllm-gpu')continue;if(POOLS[key]){ensureExtraPool(key);renderPool(key,state,558)}}$('state-note').textContent=note||''}
async function loadState(){try{const d=await fetch('/_trace/api/state',{cache:'no-store'}).then(r=>r.json());liveState=d.state;if(!pinned)renderState(d.state,'')}catch(e){}}
async function pinState(t){const seq=++pinSeq;try{const d=await fetch('/_trace/api/state?at='+t.started_at,{cache:'no-store'}).then(r=>r.json());if(seq!==pinSeq)return;pinned=true;const at=ts=>new Date(ts*1000).toLocaleTimeString();renderState(d.state,d.state?'cluster state when sent · '+at(t.started_at):'no state recorded for '+at(t.started_at)+' · history starts '+(d.history_starts?at(d.history_starts):'—'))}catch(e){}}
function unpinState(){pinSeq++;pinned=false;renderState(liveState,'')}
load();setInterval(load,2000);loadTraffic();setInterval(loadTraffic,2000);loadState();setInterval(loadState,2000)
let lastRunsKey=''
async function loadRuns(){try{const d=await fetch('/_trace/api/runs',{cache:'no-store'}).then(r=>r.json());const runs=d.runs||[];const key=runs.map(x=>x.run_id).join();if(key===lastRunsKey)return;lastRunsKey=key;$('arms').innerHTML=runs.length?runs.map(s=>{const routes=s.routes||{},served=Object.values(routes).reduce((a,b)=>a+b,0)||1,cpu=(routes['vllm-cpu']||0)/served,docs=Math.max(s.docs||1,1),hasDevice=s.gpu_s_per_doc!=null||s.gpu_device_active_s!=null||(s.gpu_busy_ms||0)>0,gpuSdoc=s.gpu_s_per_doc!=null?s.gpu_s_per_doc:s.gpu_device_active_s!=null?s.gpu_device_active_s/docs:(s.gpu_busy_ms||0)/1000/docs,gpuText=hasDevice?gpuSdoc.toFixed(2):'—',cpuNamed=((routes['vllm-gpu-small']||0)>0),viol=(s.sensitive_violations==null?'—':s.sensitive_violations);return`<tr class="${viol&&viol>0?'bad':''}"><td><b>${esc(s.config||s.arm)}</b></td><td>${esc(s.mode||'—')}</td><td>${esc(s.docs??'—')}</td><td>${esc(s.wall_s??'—')}</td><td>${Math.round(cpu*100)}%</td><td>${gpuText}${cpuNamed?'*':''}</td><td>${esc(s.fallback??0)}</td><td>${esc(s.errors??0)}</td><td>${esc(viol)}</td></tr>`}).join(''):'<tr><td colspan="9">No runs posted yet (run scripts/cluster/arms.sh or scripts/linux/arms.sh).</td></tr>';$('arms-note').textContent=runs.length?`(${runs.length} config${runs.length>1?'s':''}, latest per config)`:''}catch(e){}}
loadRuns();setInterval(loadRuns,5000)
</script></body></html>'''


def main() -> None:
    with db():
        pass
    threading.Thread(target=state_poller, daemon=True).start()
    for route, (upstream, port, _deployment, _isvc) in POOLS.items():
        threading.Thread(
            target=RelayServer(("0.0.0.0", port), upstream, route).serve_forever,
            daemon=True,
        ).start()
    for upstream, port, entry in (
        (SENSITIVE_UPSTREAM, SENSITIVE_INGRESS_PORT, "sensitive"),
        (JUDGMENT_UPSTREAM, JUDGMENT_INGRESS_PORT, "judgment"),
        (SENSITIVE_JUDGMENT_UPSTREAM, SENSITIVE_JUDGMENT_INGRESS_PORT,
         "sensitive-judgment"),
    ):
        if upstream:
            threading.Thread(
                target=RelayServer(("0.0.0.0", port), upstream, entry=entry).serve_forever,
                daemon=True,
            ).start()
    print(json.dumps({"event": "trace-ui-start", "ingress": INGRESS_PORT,
                      "sensitive_ingress": SENSITIVE_INGRESS_PORT if SENSITIVE_UPSTREAM else None,
                      "judgment_ingress": JUDGMENT_INGRESS_PORT if JUDGMENT_UPSTREAM else None,
                      "sensitive_judgment_ingress": (
                          SENSITIVE_JUDGMENT_INGRESS_PORT
                          if SENSITIVE_JUDGMENT_UPSTREAM else None),
                      "pools": {r: p[1] for r, p in POOLS.items()}}), flush=True)
    RelayServer(("0.0.0.0", INGRESS_PORT), UPSTREAM).serve_forever()


if __name__ == "__main__":
    main()
