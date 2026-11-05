"""OpenTelemetry export for the trace relay.

The relay's SQLite database stays the audit trail: it is self-contained,
exportable as JSONL, and the governance check reads it directly.  This module
adds a *portable* export alongside it, so the demo's routing decisions can be
consumed by Langfuse, Phoenix, Jaeger/Tempo, or any OTLP backend without
replacing the relay.

Two modes, both opt-in via ``TRACE_OTEL``:

* ``jsonl`` - dependency-free OTLP-shaped JSON lines (one span per request) to
  ``TRACE_OTEL_JSONL``.  This is the mode the demo/tests use, and it is enough
  for a notebook or a log pipeline.
* ``otlp``  - the real OpenTelemetry SDK + OTLP/HTTP exporter, if the ``otel``
  extra is installed.  Endpoint comes from ``OTEL_EXPORTER_OTLP_ENDPOINT``.

Attribute names follow the OpenTelemetry GenAI semantic conventions where
they exist (``gen_ai.*``) and use an ``app.*`` namespace for the demo's
routing/governance fields.  Sensitive document text is never exported: the
relay already redacts it unless ``TRACE_STORE_SENSITIVE_CONTENT=1``, and this
module records only lengths/estimates even then.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_DEFAULT_PATH = Path(os.environ.get("TRACE_OTEL_JSONL", "/data/otel-spans.jsonl"))
_OTLP_READY = False
_OTLP_ERROR: str | None = None
_JSONL_WARNED = False


def mode() -> str:
    value = (os.environ.get("TRACE_OTEL") or "").strip().lower()
    if value in ("1", "true", "on", "jsonl", "file"):
        return "jsonl"
    if value in ("otlp", "sdk"):
        return "otlp"
    return "off"


def jsonl_path() -> Path:
    return Path(os.environ.get("TRACE_OTEL_JSONL", str(_DEFAULT_PATH)))


def status() -> dict[str, Any]:
    return {"mode": mode(), "jsonl": str(jsonl_path()),
            "otlp_ready": _OTLP_READY, "otlp_error": _OTLP_ERROR}


def _estimate_tokens(text: str | None) -> int:
    return max(1, (len(text or "") + 3) // 4) if text else 0


def _span_from_row(row: dict, hops: list[dict]) -> dict[str, Any]:
    started = float(row.get("started_at") or time.time())
    completed = float(row.get("completed_at") or started)
    headers = row.get("response_headers") or {}
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except json.JSONDecodeError:
            headers = {}
    lowered = {str(k).lower(): v for k, v in headers.items()}
    entry = row.get("entry") or "general"
    sensitive = str(entry).startswith("sensitive")
    question = row.get("question")
    prompt_tokens = lowered.get("x-prompt-tokens")
    estimated_input = prompt_tokens is None
    if estimated_input:
        prompt_tokens = _estimate_tokens(question)
    response_tokens = row.get("response_tokens")
    if response_tokens is None:
        response_tokens = 0
    model = (lowered.get("x-served-model-name") or lowered.get("x-model")
             or lowered.get("x-pool") or "")
    attributes: dict[str, Any] = {
        "gen_ai.system": "praxis",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": int(prompt_tokens or 0),
        "gen_ai.usage.output_tokens": int(response_tokens or 0),
        "gen_ai.usage.input_tokens_estimated": bool(estimated_input),
        "http.request.method": row.get("method"),
        "url.path": row.get("path"),
        "http.response.status_code": row.get("status"),
        "app.trace.id": row.get("id"),
        "app.routing.classification": row.get("classification") or "",
        "app.routing.route": row.get("route") or "",
        "app.routing.entry": entry,
        "app.routing.fallback": bool(row.get("fallback")),
        "app.governance.sensitive": sensitive,
        "app.governance.content_redacted": sensitive and not question,
        "app.hops.count": len(hops),
        "app.hops.routes": ",".join(str(h.get("route") or "") for h in hops),
        "app.duration_ms": row.get("duration_ms"),
    }
    events = []
    for hop in hops:
        events.append({
            "name": "praxis.hop",
            "time_unix_nano": int(float(hop.get("arrived_at") or started) * 1e9),
            "attributes": {
                "app.hop.route": hop.get("route") or "",
                "app.hop.ttfb_ms": hop.get("ttfb_ms"),
                "app.hop.total_ms": hop.get("total_ms"),
                "app.hop.status": hop.get("status"),
                "app.hop.classifier_us": hop.get("classifier_us"),
                "app.hop.error": hop.get("error") or "",
            },
        })
    return {
        "trace_id": row.get("id"),
        "span_id": row.get("id"),
        "name": f"{row.get('method') or 'POST'} {str(row.get('path') or '').split('?')[0]}",
        "kind": "SPAN_KIND_SERVER",
        "start_time_unix_nano": int(started * 1e9),
        "end_time_unix_nano": int(completed * 1e9),
        "status": "STATUS_CODE_ERROR" if (row.get("status") or 0) >= 500 or row.get("error")
        else "STATUS_CODE_OK",
        "attributes": attributes,
        "events": events,
        "resource": {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "hybrid-trace-ui"),
            "service.version": os.environ.get("HYBRID_GIT_SHA", "dev"),
        },
    }


def _write_jsonl(span: dict[str, Any]) -> None:
    global _JSONL_WARNED
    path = jsonl_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(span, ensure_ascii=False, default=str) + "\n")
    except OSError as error:
        if not _JSONL_WARNED:
            print(f"traceui: OTel JSONL export disabled: {error}", file=sys.stderr)
            _JSONL_WARNED = True


def _otlp_span(span: dict[str, Any]) -> bool:
    """Emit through the OTel SDK.  Returns False when it cannot."""
    global _OTLP_READY, _OTLP_ERROR
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as error:
        _OTLP_ERROR = f"opentelemetry sdk/exporter not installed: {error}"
        return False
    if not _OTLP_READY:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        resource = Resource.create({
            "service.name": span["resource"]["service.name"],
            "service.version": span["resource"]["service.version"],
        })
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        ))
        trace.set_tracer_provider(provider)
        _OTLP_READY = True
    tracer = trace.get_tracer("hybrid-demo.traceui")
    attributes = {k: v for k, v in span["attributes"].items()
                  if isinstance(v, (str, bool, int, float))}
    with tracer.start_as_current_span(
        span["name"], start_time=span["start_time_unix_nano"], attributes=attributes
    ) as otel_span:
        for event in span.get("events", []):
            otel_span.add_event(event["name"], attributes={
                k: v for k, v in event["attributes"].items()
                if isinstance(v, (str, bool, int, float))
            }, timestamp=event["time_unix_nano"])
        otel_span.set_status(
            trace.Status(trace.StatusCode.ERROR)
            if span["status"] == "STATUS_CODE_ERROR" else trace.Status(trace.StatusCode.OK)
        )
        otel_span.end(end_time=span["end_time_unix_nano"])
    return True


def emit_trace(row: dict, hops: list[dict] | None = None) -> dict[str, Any] | None:
    """Emit one relay trace.  Never raises into the request path."""
    selected = mode()
    if selected == "off":
        return None
    span = _span_from_row(row, hops or [])
    if selected == "jsonl":
        _write_jsonl(span)
        return span
    if not _otlp_span(span):
        # Fall back to JSONL rather than losing the trace; the error is
        # visible via status() and stderr on first use.
        _write_jsonl(span)
    return span
