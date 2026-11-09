"""Turn a scored run into the shape the stage UI renders.

Nothing here re-scores anything: extraction verdicts, risk matches and the
report breakdown all come from ``evalkit.score``, so a number on the page and
a number in ``metrics.json`` cannot disagree. The only new work is locating a
model's quote inside the source document, which the UI needs to show *where*
the clause was found and whether the quote really is in the text.

    build_payload(run_dir, data_dir) -> dict   # whole run, for replay
    build_document(...)             -> dict   # one document, for live mode
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from ..configs import catalog, meta_for, name_for_key
from ..evalkit.score import (
    docs_fully_correct,
    extraction_scores,
    majority_risk_baseline,
    report_checks,
    step_accuracy,
    text_matches,
)

EXCERPT_CHARS = 320
CLAIM_CHARS = 1600


def _span(text: str, needle: str | None) -> tuple[int, int] | None:
    """Locate a quote in the source, tolerating case and reflowed whitespace.

    A plain ``find`` would mark a true quote unverifiable whenever the model
    re-wrapped a line — which it does constantly in long clauses. The words
    must still appear in order and contiguously, which is the property a
    reviewer checking a quote actually cares about.
    """
    if not text or not needle:
        return None
    needle = needle.strip()
    if not needle:
        return None
    direct = text.lower().find(needle.lower())
    if direct >= 0:
        return direct, direct + len(needle)
    words = [re.escape(w) for w in needle.split()]
    match = re.search(r"\s+".join(words), text, re.IGNORECASE)
    return (match.start(), match.end()) if match else None


def _excerpt(text: str, needle: str | None, width: int = EXCERPT_CHARS) -> dict | None:
    span = _span(text, needle)
    if span is None:
        return None
    start, end = span
    return {
        "before": text[max(0, start - width):start],
        "hit": text[start:end],
        "after": text[end:end + width],
        "cut_before": start > width,
        "cut_after": end + width < len(text),
    }


def _clip(value: str | None, limit: int = CLAIM_CHARS) -> str:
    if not value:
        return ""
    return value if len(value) <= limit else value[:limit] + " …"


def _verdict(truth: str | None, found: bool, text: str | None,
             scored: bool = True) -> str:
    """Name the outcome the way the scorer counts it.

    ``scored=False`` is a document with no annotations (a live submission):
    there is nothing to be right or wrong about, so the quote is still
    verified against the source but no correctness verdict is claimed.
    """
    if not scored:
        return "unscored"
    if truth is None:
        return "false_positive" if found else "correct_absent"
    if not found:
        return "missed"
    return "correct" if text_matches(text, truth) else "wrong_quote"


def _trace(rec: dict | None) -> dict:
    """Which pool answered, under which governance entry, and on what trace.

    A direct run carries none of this: the absence is the finding, so it is
    passed through as ``None`` rather than being filled with a guess. The
    complexity label is the *annotated* label, not the gateway's decision.
    """
    rec = rec or {}
    headers = rec.get("headers") or {}
    return {
        "route": headers.get("x-demo-route") or rec.get("route"),
        "entry": headers.get("x-demo-entry") or rec.get("entry"),
        "trace_id": headers.get("x-praxis-trace-id") or rec.get("trace_id"),
        "sensitive": rec.get("true_sensitive"),
        "label": rec.get("true_complexity"),
        # The label praxis actually applied, which is a decision about the
        # request; ``label`` above is the annotation the run was scored
        # against. Showing only one of the two invites reading it as the other.
        "decision": headers.get("x-demo-classification"),
        # What the server said answered this call. Absent for runs recorded
        # before the field existed, and for a pool that omits it.
        "model": rec.get("response_model"),
    }


def _request(rec: dict | None) -> dict | None:
    """The messages actually sent, for a "what we asked" disclosure."""
    body = (rec or {}).get("request_body") or {}
    messages = body.get("messages") or []
    if not messages:
        return None
    return {
        "model": body.get("model"),
        "max_tokens": body.get("max_tokens"),
        "temperature": body.get("temperature"),
        "messages": [{"role": m.get("role"),
                      # A stored request carries the whole document; the page
                      # only needs enough to show the instruction and the shape.
                      "content": _clip(m.get("content"), 4000)}
                     for m in messages],
    }


def _extract_payload(clause: str, rec: dict | None, truth: str | None,
                     doc_text: str, scored: bool = True) -> dict:
    content = (rec or {}).get("content") or {}
    text = content.get("text") or ""
    found = bool(content.get("found"))
    # A failed call produced no answer, so it cannot earn the "correct absent"
    # credit a served call gets for saying "not found" — it is a missing field.
    served = bool(rec) and rec.get("outcome") == "served"
    verdict = _verdict(truth, found, text, scored) if served else "missing"
    return {
        "clause": clause,
        "found": found,
        "claim": _clip(text),
        "claim_truncated": len(text) > CLAIM_CHARS,
        "ref": content.get("ref") or "",
        "latency_ms": (rec or {}).get("latency_ms"),
        "outcome": (rec or {}).get("outcome", "missing"),
        "truth": _clip(truth),
        "verdict": verdict,
        # Independent of the scorer: is the quoted string actually in the file?
        "verified": bool(_span(doc_text, text)) if text else None,
        "excerpt": _excerpt(doc_text, text),
        "truth_excerpt": _excerpt(doc_text, truth),
        "usage": (rec or {}).get("usage"),
        "request": _request(rec),
        **_trace(rec),
    }


def build_document(doc_id: str, doc_text: str, records: list[dict],
                   truth_doc: dict, clause_keys: list[str],
                   scored: bool = True) -> dict:
    """One document's stages, predictions, truth and per-field verdicts.

    ``scored=False`` for a document submitted at runtime: the annotations are
    absent, so the page shows what the model produced and whether each quote
    really occurs in the text, but claims no correctness and contributes no
    fields to the per-document meter.
    """
    by_step = {r["step"]: r for r in records if r.get("doc_id") == doc_id}
    truth_clauses = truth_doc.get("clauses") or {}
    truth_type = truth_doc.get("type")
    truth_risk = truth_doc.get("risk")

    classify_rec = by_step.get("classify")
    classify_content = (classify_rec or {}).get("content") or {}
    classify = None
    if classify_rec:
        type_match = (None if truth_type is None or not scored
                      else classify_content.get("type") == truth_type)
        classify = {
            "type": classify_content.get("type"),
            "initial_term_months": classify_content.get("initial_term_months"),
            "ref": classify_content.get("ref") or "",
            "latency_ms": classify_rec.get("latency_ms"),
            "outcome": classify_rec.get("outcome", "missing"),
            "truth": truth_type,
            "match": type_match,
            "usage": classify_rec.get("usage"),
            "request": _request(classify_rec),
            **_trace(classify_rec),
        }

    extracts = [_extract_payload(c, by_step.get(f"extract:{c}"),
                                 truth_clauses.get(c), doc_text, scored)
                for c in clause_keys]

    risk_rec = by_step.get("risk")
    risk_content = (risk_rec or {}).get("content") or {}
    risk = None
    if risk_rec:
        risk = {
            "risk": risk_content.get("risk"),
            "reasons": risk_content.get("reasons") or [],
            "latency_ms": risk_rec.get("latency_ms"),
            "outcome": risk_rec.get("outcome", "missing"),
            "truth": truth_risk,
            "match": None if not scored else risk_content.get("risk") == truth_risk,
            "usage": risk_rec.get("usage"),
            "request": _request(risk_rec),
            **_trace(risk_rec),
        }

    checks: list[bool] = []
    if scored:
        if classify is not None and classify["match"] is not None:
            checks.append(bool(classify["match"]))
        for e in extracts:
            checks.append(e["verdict"] in ("correct", "correct_absent"))
        if risk is not None:
            checks.append(bool(risk["match"]))

    routes = Counter(t["route"] for t in (_trace(r) for r in by_step.values())
                     if t["route"])
    # The extraction stage header must count only extraction calls. Using the
    # document-wide route count there mixed classify and risk into a row that
    # sits above the extraction cards (e.g. "vllm-cpu x5, vllm-gpu x1" for a
    # document with four CPU extracts, one CPU classify and one GPU risk).
    extract_routes = Counter(e["route"] for e in extracts if e["route"])
    return {
        "doc_id": doc_id,
        "chars": len(doc_text),
        "scored": scored,
        "type_truth": truth_type,
        "risk_truth": truth_risk,
        "classify": classify,
        "extracts": extracts,
        "risk": risk,
        # Where this document's calls actually went. Empty for a direct run:
        # that is the distinction the page has to make visible, not hide.
        "routes": dict(routes),
        # The same count, scoped to the cards rendered under "Extract clauses".
        "extract_routes": dict(extract_routes),
        "sensitive": any(r.get("true_sensitive") for r in by_step.values()),
        "correct_fields": sum(checks),
        "fields_total": len(checks),
        "fully_correct": bool(checks) and all(checks),
    }


def _document_order(truth_docs: dict[str, dict], run_ids: set[str]) -> list[str]:
    """Manifest order, deduplicated (CUAD ships one repeated doc_id)."""
    order: list[str] = []
    for did in truth_docs:
        if did in run_ids and did not in order:
            order.append(did)
    order += sorted(run_ids - set(order))
    return order


def _routing(records: list[dict]) -> dict:
    """Aggregate the routes the gateway actually applied.

    Empty for a direct run. The page renders that emptiness as a statement
    about the run ("no gateway in this path"), not as missing data to fill in.

    A call that failed carries no route header because there was no successful
    response to read one from — not because the gateway declined to route it.
    Those calls are counted apart from the routes, so a 504 is never shown as
    evidence of a routing decision that never happened.
    """
    pairs = [(r, _trace(r)) for r in records]
    sensitive = [(r, t) for r, t in pairs if t["sensitive"]]
    answered = [(r, t) for r, t in sensitive if t["route"]]
    silent = [r for r, t in pairs if not t["route"]]
    return {
        "routes": dict(Counter(t["route"] for _, t in pairs if t["route"])),
        "entries": dict(Counter(t["entry"] for _, t in pairs if t["entry"])),
        # The labels praxis applied, next to the pools it chose for them; the
        # pair is the routing decision this page exists to show.
        "labels": dict(Counter(t["decision"] for _, t in pairs if t["decision"])),
        "models": dict(Counter(t["model"] for _, t in pairs if t["model"])),
        "trace_count": sum(1 for _, t in pairs if t["trace_id"]),
        "no_route": len(silent),
        "no_route_failed": sum(1 for r in silent if r.get("outcome") != "served"),
        "sensitive_steps": len(sensitive),
        "sensitive_routes": dict(Counter(t["route"] for _, t in answered)),
        "sensitive_no_route": len(sensitive) - len(answered),
    }


def _pool_load(run_path: Path) -> dict:
    """Per-pool device work, when the run recorded it.

    A direct run's ``metrics.json`` is its scored result; a through-stack run's
    is pool telemetry. Detect the shape instead of assuming what the filename
    means. Tokens per device-second is only computed when both sides exist —
    dividing by wall time would flatter a pool that was idle for most of the run.
    """
    path = run_path / "metrics.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except ValueError:
        return {}
    pools = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        if "samples" not in value and "generation_tokens" not in value:
            continue
        gen, active = value.get("generation_tokens"), value.get("device_active_s")
        pools[name] = {
            "prompt_tokens": value.get("prompt_tokens"),
            "generation_tokens": gen,
            "device_active_s": active,
            "elapsed_s": value.get("elapsed_s"),
            "method": value.get("method"),
            "gen_tps": round(gen / active, 1) if gen and active else None,
        }
    return pools


def _perf(perf_dir: Path) -> list[dict]:
    """The concurrency sweep results that sit beside the runs, if present."""
    out: list[dict] = []
    if not perf_dir.is_dir():
        return out
    for path in sorted(perf_dir.glob("*.json")):
        try:
            d = json.loads(path.read_text())
        except ValueError:
            continue
        parts = path.stem.split("-")
        hardware = "cpu" if parts[0] == "cpu" else "gpu"
        tail = parts[-1]
        if not tail.startswith("c") or not tail[1:].isdigit():
            continue
        out.append({
            "hardware": hardware,
            "set": "-".join(parts[1:-1] if hardware == "cpu" else parts[:-1]),
            "concurrency": int(tail[1:]),
            "prompts": d.get("num_prompts"),
            "failed": d.get("failed"),
            "ttft_p50": d.get("p50_ttft_ms"),
            "ttft_p95": d.get("p95_ttft_ms"),
            "tpot_p50": d.get("p50_tpot_ms"),
            "tpot_p95": d.get("p95_tpot_ms"),
            "e2e_p50": d.get("p50_e2el_ms"),
            "e2e_p95": d.get("p95_e2el_ms"),
            "output_tps": d.get("output_throughput"),
            "input_tokens": d.get("total_input_tokens"),
            "output_tokens": d.get("total_output_tokens"),
        })
    return sorted(out, key=lambda x: (x["hardware"], x["set"], x["concurrency"]))


def _pool_hardware(summary: dict) -> list[dict]:
    """What actually served the run, read from its recorded pool map.

    Only routes that answered are listed: a pool map can describe every pool
    the deployment configured, while the run used two of them. A run with no
    route counts (a direct run, or evidence recorded before pool maps) gets an
    empty list, and the page says "hardware not recorded" rather than inferring
    hardware from a route name.
    """
    pools = summary.get("pools") or {}
    routes = summary.get("routes") or {}
    if not isinstance(pools, dict) or not isinstance(routes, dict):
        return []
    out = []
    for route in routes:
        info = pools.get(route)
        if not isinstance(info, dict):
            continue
        out.append({
            "route": route,
            "model": info.get("model"),
            "hardware": info.get("hardware"),
            "deployment": info.get("deployment"),
        })
    return out


def _provenance(summary: dict) -> str:
    """Whether a run's numbers may be presented as measured.

    Default to ``example``: a missing field must never promote a run. The
    first measured environment is ROSA; an explicit ``provenance`` field wins
    so a future measured stack can say so without changing this rule.
    """
    declared = summary.get("provenance")
    if declared in ("example", "measured"):
        return declared
    environment = str(summary.get("environment") or "").strip().lower()
    # Exact name only: "pre-rosa" or "not-rosa" must not promote a run.
    return "measured" if environment == "rosa" else "example"


def build_payload(run_dir: str | Path, data_dir: str | Path,
                  live_enabled: bool = False) -> dict:
    run_path = Path(run_dir)
    data_path = Path(data_dir)
    run = json.loads((run_path / "results.json").read_text())
    manifest = json.loads((data_path / "ground_truth.json").read_text())
    truth_docs = {d["doc_id"]: d for d in manifest["documents"]}
    records = run.get("records", [])
    # CUAD's manifest can list the same truncated id twice, and the batch
    # runner then executes that contract twice. One card cannot show two
    # passes without double-counting, so the first pass is kept and the
    # repeat is stated in the header instead of silently inflating the score.
    seen: set[tuple] = set()
    unique: list[dict] = []
    repeated: set[str] = set()
    for rec in records:
        key = (rec.get("doc_id"), rec.get("step"))
        if key in seen:
            repeated.add(rec.get("doc_id") or "")
            continue
        seen.add(key)
        unique.append(rec)
    duplicate_docs = len(repeated)
    records = unique
    clause_keys = manifest.get("clause_keys")
    if not clause_keys:
        clause_keys = sorted({r["step"].split(":", 1)[1] for r in records
                              if r["step"].startswith("extract:")})

    run_ids = {r["doc_id"] for r in records if r.get("doc_id")}
    documents = []
    for doc_id in _document_order(truth_docs, run_ids):
        path = data_path / "documents" / f"{doc_id}.txt"
        text = path.read_text() if path.exists() else ""
        documents.append(build_document(
            doc_id, text, [r for r in records if r.get("doc_id") == doc_id],
            truth_docs.get(doc_id, {}), clause_keys))

    summary = run.get("summary") or {}
    levels = Counter(truth_docs[d].get("risk", "low") for d in run_ids if d in truth_docs)
    majority_level, majority_count = (levels.most_common(1)[0] if levels else (None, 0))
    metrics = {
        "extraction": extraction_scores(records, truth_docs),
        "type_accuracy": step_accuracy(records, truth_docs, "classify", "type"),
        "risk_accuracy": step_accuracy(records, truth_docs, "risk", "risk"),
        "report_accuracy": None,
        "docs_fully_correct": docs_fully_correct(records, truth_docs),
        "majority_risk_baseline": majority_risk_baseline(truth_docs, run_ids),
        # "Most common" is high on this document set, not low; the UI must say
        # which level the baseline is answering, or the comparison misleads.
        "majority_risk_level": majority_level,
        "majority_risk_count": majority_count,
    }
    report = report_checks(records, truth_docs)
    metrics["report_accuracy"] = None if report is None else report["score"]
    routing = _routing(records)
    # The runner already counts these; copying them beats recomputing a
    # safety property from the same records with a second implementation.
    for key in ("sensitive_on_approved", "sensitive_violations",
                "fallback", "errors"):
        if key in summary:
            routing[key] = summary[key]

    medians = {}
    for step in ["classify", "risk"] + [f"extract:{c}" for c in clause_keys]:
        values = sorted(r["latency_ms"] for r in records
                        if r["step"] == step and isinstance(r.get("latency_ms"), (int, float)))
        if values:
            medians[step] = round(values[len(values) // 2], 1)

    # Provenance is a claim about the evidence, so it is never inferred from
    # the checkout the page is served from. A run is an example unless the
    # runner says otherwise; ROSA is the first environment whose runs count
    # as measured. Old runs therefore cannot be quoted as measured by accident.
    environment = summary.get("environment")
    provenance = _provenance(summary)
    # The canonical record names the policy and its behavior. Hardware still
    # comes only from the run's pool map, never from the config.
    config = summary.get("config") or name_for_key(summary.get("arm"))
    config_meta = meta_for(config or summary.get("arm"))
    pool_hardware = _pool_hardware(summary)

    return {
        "meta": {
            "run": run_path.name,
            "arm": summary.get("arm"),
            "config": config,
            "config_meta": config_meta,
            "pool_hardware": pool_hardware,
            "mode": summary.get("mode"),
            "docs": len(documents),
            "endpoint": summary.get("endpoint"),
            "model": summary.get("model") or summary.get("served_model"),
            "served_model": summary.get("served_model"),
            "hardware": summary.get("hardware"),
            "thinking": bool(summary.get("thinking")),
            "wall_s": summary.get("wall_s"),
            # Which checkpoints answered, as the servers named them. Empty on
            # runs recorded before the response model was captured.
            "models": summary.get("models") or {},
            "stage_models": summary.get("stage_models") or {},
            # The runner's own document count, before the duplicate fold.
            "run_docs": summary.get("docs"),
            "duplicate_docs": duplicate_docs,
            # Whether the calls were routed at all. This is the run's defining
            # property, so it is stated in the header rather than inferred from
            # an empty panel.
            "gateway": bool(routing["routes"]),
            # Where and when, as recorded by the runner. Present only on runs
            # made after provenance capture was added; the page says nothing
            # rather than guessing for older ones.
            "provenance": provenance,
            "environment": environment,
            "cluster": summary.get("cluster"),
            "git_sha": summary.get("git_sha"),
            "git_dirty": summary.get("git_dirty"),
            "started_at": summary.get("started_at"),
            "concurrency": summary.get("concurrency"),
            "timeout_s": summary.get("timeout_s"),
            # route → {model, hardware, deployment}, supplied at deploy time.
            "pools": summary.get("pools") or {},
        },
        "metrics": metrics,
        "medians_ms": medians,
        "report": report,
        "routing": routing,
        "pool_load": _pool_load(run_path),
        "perf": _perf(run_path.parent / "perf"),
        "clause_keys": clause_keys,
        "documents": documents,
        # The catalog the page reads names from, so the UI carries no second
        # copy of the config-name map.
        "configs": catalog(),
        "live_enabled": bool(live_enabled),
    }
