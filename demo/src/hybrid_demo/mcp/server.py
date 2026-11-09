"""MCP facade for the governed contract-review pipeline.

This is a thin adapter over the same ingress relays and Praxis entries the
deterministic ``hybrid-run`` client uses. It never calls a pool's vLLM endpoint
directly: a sensitive document therefore stays on the approved entries, and
every call is visible to the gateway and the trace relay.

The measured reference path is unchanged. This surface is functional and
agent-facing; it is not itself a measured arm until it has a run record.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..classifier import detect_sensitivity
from ..configs import CONFIG_ALIASES, GOVERNED_CONFIGS
from ..pii import detect, resolve_detector
from ..pipeline.generate import CLAUSE_KEYS
from ..pipeline.review import (
    ReviewRun,
    extract_body,
    report_body,
    risk_body,
    run_arm,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATA_DIR = REPO_ROOT / "demo" / "data" / "synthetic"

mcp = FastMCP(
    "governed-contract-review",
    instructions=(
        "Contract review over the governed Praxis gateway. Read a document with "
        "contract://{doc_id}; run the full deterministic review with "
        "review_document. The stage tools compose the same governed steps. "
        "Every model call goes through the ingress relays, never a pool directly."
    ),
)


def _data_dir() -> Path:
    raw = os.environ.get("HYBRID_DATA")
    path = Path(raw).expanduser() if raw else DEFAULT_DATA_DIR
    return path.resolve()


def _document(doc_id: str) -> tuple[dict[str, Any], str, Path]:
    """Return (manifest entry, text, data dir) or raise for an unknown id."""
    data_dir = _data_dir()
    manifest = json.loads((data_dir / "ground_truth.json").read_text())
    for doc in manifest.get("documents", []):
        if doc.get("doc_id") == doc_id:
            text = (data_dir / "documents" / f"{doc_id}.txt").read_text()
            return doc, text, data_dir
    raise ValueError(f"unknown document {doc_id!r} in {data_dir}")


def _env_url(name: str) -> str | None:
    raw = os.environ.get(name)
    return raw.rstrip("/") if raw else None


def _gateways() -> dict[str, str | None]:
    general = _env_url("HYBRID_GATEWAY")
    if not general:
        raise RuntimeError(
            "HYBRID_GATEWAY is not set; point it at the trace-ui general "
            "ingress, not a pool"
        )
    return {
        "general": general,
        "sensitive": _env_url("HYBRID_SENSITIVE_GATEWAY"),
        "judgment": _env_url("HYBRID_JUDGMENT_GATEWAY"),
        "sensitive_judgment": _env_url("HYBRID_SENSITIVE_JUDGMENT_GATEWAY"),
    }


def _config() -> str:
    raw = os.environ.get("HYBRID_CONFIG", "hybrid").strip().lower()
    return CONFIG_ALIASES.get(raw, raw)


def _new_run() -> ReviewRun:
    gateways = _gateways()
    config = _config()
    governance = config in GOVERNED_CONFIGS
    if governance and not gateways["sensitive"]:
        raise RuntimeError(
            "HYBRID_SENSITIVE_GATEWAY is required for the governed hybrid config"
        )
    return ReviewRun(
        gateway_base=gateways["general"],
        sensitive_base=gateways["sensitive"],
        concurrency=1,
        governance=governance,
        timeout=float(os.environ.get("HYBRID_TIMEOUT", "600")),
        token=os.environ.get("TRACE_API_TOKEN"),
        judgment_base=gateways["judgment"],
        sensitive_judgment_base=gateways["sensitive_judgment"],
    )


def _pools() -> dict | None:
    raw = os.environ.get("HYBRID_POOLS")
    if not raw:
        return None
    text = Path(raw[1:]).read_text() if raw.startswith("@") else raw
    value = json.loads(text)
    return value if isinstance(value, dict) else None


async def _stage(
    step: str,
    body: dict[str, Any],
    doc_id: str | None,
    screened: bool,
    complexity: str,
) -> dict[str, Any]:
    """Make one governed stage call and return its traceable result."""
    run = _new_run()
    try:
        client, entry = run.entry_for_step(screened, step)
        if client is None:
            raise RuntimeError(f"no gateway client for entry {entry!r}")
        record = await run.call(client, entry, step, body, doc_id, complexity, screened)
        return {
            "step": step,
            "entry": entry,
            "outcome": record.get("outcome"),
            "route": (record.get("headers") or {}).get("x-demo-route"),
            "model": record.get("response_model"),
            "latency_ms": record.get("latency_ms"),
            "content": record.get("content"),
            "error": record.get("error"),
        }
    finally:
        await run.close()


def _review_payload(
    doc_id: str, summary: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    stages: dict[str, Any] = {}
    for record in records:
        step = record.get("step")
        if not step:
            continue
        stages[step] = {
            "outcome": record.get("outcome"),
            "route": (record.get("headers") or {}).get("x-demo-route"),
            "entry": record.get("entry"),
            "model": record.get("response_model"),
            "latency_ms": record.get("latency_ms"),
            "content": record.get("content"),
        }
    return {"doc_id": doc_id, "summary": summary, "stages": stages}


@mcp.resource("contract://{doc_id}")
def contract(doc_id: str) -> str:
    """Document text plus its manifest annotations, as JSON."""
    doc, text, _ = _document(doc_id)
    return json.dumps({"doc": doc, "text": text}, ensure_ascii=False)


@mcp.tool()
def screen_document(text: str) -> dict[str, Any]:
    """Run the configured PII screen over text without calling a model.

    The default is the regex screen. A finding means the document is routed to
    the approved entries; it is a routing signal, not a redaction.
    """
    detector = resolve_detector()
    findings = detect(text, detector)
    return {
        "sensitive": bool(findings),
        "detector": detector,
        "findings": [finding.as_dict() for finding in findings],
    }


@mcp.tool()
async def review_document(doc_id: str) -> dict[str, Any]:
    """Run the full deterministic review (screen -> classify -> extract -> risk -> report)."""
    _, _, data_dir = _document(doc_id)
    gateways = _gateways()
    config = _config()
    try:
        with tempfile.TemporaryDirectory(prefix="gci-mcp-") as out_dir:
            summary = await run_arm(
                gateway_base=gateways["general"],
                sensitive_base=gateways["sensitive"],
                data_dir=str(data_dir),
                out_dir=out_dir,
                arm=config,
                docs_limit=None,
                concurrency=1,
                timeout=float(os.environ.get("HYBRID_TIMEOUT", "600")),
                token=os.environ.get("TRACE_API_TOKEN"),
                environment=os.environ.get("HYBRID_ENVIRONMENT"),
                cluster=os.environ.get("HYBRID_CLUSTER"),
                pools=_pools(),
                doc_ids=[doc_id],
                judgment_base=gateways["judgment"],
                sensitive_judgment_base=gateways["sensitive_judgment"],
                run_kind="mcp",
            )
            results = json.loads((Path(out_dir) / "results.json").read_text())
    except SystemExit as error:
        raise RuntimeError(str(error)) from error
    return _review_payload(doc_id, summary, results.get("records", []))


@mcp.tool()
async def extract_clause(doc_id: str, clause: str) -> dict[str, Any]:
    """Extract one clause from a document through the governed extract entry."""
    if clause not in CLAUSE_KEYS:
        raise ValueError(f"unknown clause {clause!r}; choose {', '.join(CLAUSE_KEYS)}")
    _, text, _ = _document(doc_id)
    screened = detect_sensitivity(text)
    body = extract_body(text, clause.replace("_", " "))
    return await _stage(f"extract:{clause}", body, doc_id, screened, "SIMPLE")


@mcp.tool()
async def assess_risk(
    doc_id: str, clauses: dict[str, str | None]
) -> dict[str, Any]:
    """Assess risk from already-extracted clauses through the judgment entry."""
    unknown = sorted(set(clauses) - set(CLAUSE_KEYS))
    if unknown:
        raise ValueError(f"unknown clauses {unknown}; choose {', '.join(CLAUSE_KEYS)}")
    _, text, _ = _document(doc_id)
    screened = detect_sensitivity(text)
    return await _stage("risk", risk_body(clauses, screened), doc_id, screened, "COMPLEX")


@mcp.tool()
async def build_report(
    documents: list[dict[str, Any]], has_data_protection: bool = True
) -> dict[str, Any]:
    """Build the cross-document report from per-document review lines."""
    if not documents:
        raise ValueError("documents must contain at least one document")
    lines = [f"- {json.dumps(doc, ensure_ascii=False)}" for doc in documents]
    screened = detect_sensitivity("\n".join(lines))
    body = report_body(lines, len(documents), has_data_protection)
    return await _stage("report", body, None, screened, "REASONING")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
