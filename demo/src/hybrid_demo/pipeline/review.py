from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from ..classifier import detect_sensitivity
from ..common import api_key_for_step, normalize_text, parse_pools, run_provenance
from ..configs import (
    CONFIG_ALIASES,
    CONFIG_MODES,
    CONFIG_NAMES,
    GOVERNED_CONFIGS,
    resolve_config,
)
from ..evalkit.score import gpu_device_seconds, jurisdiction
from ..routes import APPROVED_ROUTES, GPU_ROUTES
from .generate import CLAUSE_KEYS
from .risk import DATA_PROTECTION_KEY, rubric_text
from .schemas import apply as apply_schema

# Steps whose work is judgment rather than extraction. The praxis config
# routes them to a GPU pool by X-Demo-Step, so the split is a policy decision
# by step type, not the classifier's guess. Extraction stays on the CPU: CUAD
# clauses on the specialist, data protection on the CPU MoE.
JUDGMENT_STEPS = {"risk", "report"}

# Thinking mode (the quality lever tested in FINDINGS §8): Qwen3.5 emits
# <think/>...</think/> blocks that consume the token budget before the
# answer, so each step's max_tokens scales when HYBRID_THINKING=1 and the
# parser strips think blocks before JSON extraction. Default unchanged.
#
# The thinking budgets are sized from measurement, not guesswork. On
# Qwen3.5-35B-A3B (4x A10G, TP4, 2026-11-03) a 1500-token extract budget
# finished with `finish_reason=length` and an EMPTY answer - the whole
# budget went to the think block - while 6000 tokens finished at 3526 and
# returned valid JSON. A short budget does not make thinking cheaper, it
# makes the arm measure truncation. Extract prompts are ~2.7k tokens, so
# 8000 keeps prompt + answer inside --max-model-len 16384.
#
# The report step is the exception: its prompt is ~8.7k tokens over 19
# documents, leaving <7.7k to generate, and 7661 tokens still ended in
# `finish_reason=length` with no answer. No client-side budget completes
# a thinking report at max-model-len 16384; raise the server's context
# window before reading anything into report accuracy in this mode.
THINKING = os.environ.get("HYBRID_THINKING", "") == "1"
STEP_TOKENS = {
    "classify": (200, 4000),
    "extract": (300, 8000),
    "risk": (300, 8000),
    "report": (1200, 3000),
}


def _gen_params(step: str) -> dict:
    base_tokens, thinking_tokens = STEP_TOKENS[step]
    if THINKING:
        return {"max_tokens": thinking_tokens,
                "chat_template_kwargs": {"enable_thinking": True}}
    return {"max_tokens": base_tokens,
            "chat_template_kwargs": {"enable_thinking": False}}

SYSTEM_PROMPT = (
    "You are a contract-analysis assistant for a legal-operations team. "
    "Answer with compact JSON only, no prose."
)

CAPTURE_HEADERS = (
    "x-demo-classification",
    "x-demo-route",
    "x-praxis-fallback",
    "x-llm-d-sc-latency-us",
    "x-busy-ms",
    "x-queue-ms",
    "x-pool",
    "x-praxis-trace-id",
    "x-demo-entry",
)


def classify_body(doc_text: str) -> dict:
    return apply_schema({
        "model": "Semantic-Qwen",
        "temperature": 0,
        **_gen_params("classify"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Classify the following contract by type and state its initial term.\n\n"
                    f"{doc_text}\n\n"
                    'Return JSON: {"type": "MSA|NDA|SOW|DPA", "initial_term_months": N, "ref": "..."}'
                ),
            },
        ],
    }, "classify")


def extract_body(doc_text: str, clause: str) -> dict:
    return apply_schema({
        "model": "Semantic-Qwen",
        "temperature": 0,
        **_gen_params("extract"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Extract the {clause} clause from the following contract. "
                    "If the clause is absent, say not found.\n\n"
                    f"{doc_text}\n\n"
                    f'Return JSON: {{"clause": "{clause}", "found": true, "text": "...", "ref": "..."}}'
                ),
            },
        ],
    }, "extract")


def risk_body(clauses: dict, sensitive: bool) -> dict:
    clauses_json = json.dumps(
        {k: {"found": bool(v), "text": v} for k, v in clauses.items()}, ensure_ascii=False
    )
    has_data_protection = DATA_PROTECTION_KEY in clauses
    note = "This document contains personal data (see Schedule 3)." if sensitive else ""
    return apply_schema({
        "model": "Semantic-Qwen",
        "temperature": 0,
        **_gen_params("risk"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Assess the overall risk of this contract given the extracted clauses below. "
                    f"{rubric_text(has_data_protection)}\n"
                    f"{note}\n"
                    f"Extracted clauses: {clauses_json}\n\n"
                    'Return JSON: {"risk": "low|medium|high", "reasons": ["..."], "ref": "..."}'
                ),
            },
        ],
    }, "risk")


def report_body(doc_lines: list[str], count: int,
                has_data_protection: bool = True) -> dict:
    dp_request = (", count contracts missing a data-protection clause"
                  if has_data_protection else "")
    dp_field = (', "missing_data_protection": N' if has_data_protection else "")
    return apply_schema({
        "model": "Semantic-Qwen",
        "temperature": 0,
        **_gen_params("report"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Produce the cross-document risk report across all {count} documents. "
                    "Synthesise and compare the per-document extractions below, derive the aggregate "
                    "counts, and prove the ranking: count contracts by risk level, count governing "
                    f"laws{dp_request}, and name the highest-risk contract reference. If several "
                    "contracts tie for highest risk, name the lexicographically smallest reference.\n\n"
                    + "\n".join(doc_lines)
                    + "\n\nReturn JSON: {\"documents\": N, \"risk_counts\": {...}, "
                    "\"governing_law_counts\": {...}"
                    + dp_field
                    + ", \"highest_risk_ref\": \"...\"}"
                ),
            },
        ],
    }, "report")


def report_rollup(rows: list[dict], has_data_protection: bool) -> dict:
    """Aggregate the report from the pipeline's own per-document rows.

    The report prompt asks a model to count every row. That is arithmetic, not
    judgment, and it gets worse as the batch grows: the 2026-11-10 200-document
    run returned 32 missing-data-protection contracts against 52, even though
    the extractor had labelled every row correctly. The pipeline already has
    the structured rows, so compute the counts here and keep the model call for
    the synthesis/provenance path. The scorer should measure propagation from
    extraction and risk, not whether a model can count 200 JSON lines.
    """
    risk_counts = {"low": 0, "medium": 0, "high": 0}
    law_counts: dict[str, int] = {}
    high_ids: list[str] = []
    missing_dp = 0
    for row in rows:
        risk = row.get("risk") or "unknown"
        risk_counts[risk] = risk_counts.get(risk, 0) + 1
        law = row.get("governing_law")
        key = jurisdiction(law) or (
            "missing"
            if law in (None, "", "missing", "unknown")
            else normalize_text(str(law))
        )
        law_counts[key] = law_counts.get(key, 0) + 1
        if not row.get("data_protection"):
            missing_dp += 1
        if risk == "high":
            high_ids.append(row["ref"])
    result = {
        "documents": len(rows),
        "risk_counts": risk_counts,
        "governing_law_counts": law_counts,
        "highest_risk_ref": sorted(high_ids)[0] if high_ids else None,
    }
    if has_data_protection:
        result["missing_data_protection"] = missing_dp
    return result


def parse_content(content: str | None) -> dict:
    """Parse a model answer into a dict.

    Real models routinely wrap JSON in markdown fences or add prose around
    it, so: strip fences, then parse the outermost {...} object. Falls back
    to {"raw": ...} so scoring can see what the model actually said.
    """
    if not content:
        return {}
    text = content.strip()
    # Qwen3.5 thinking mode: drop the reasoning block before parsing. An
    # unterminated block means the budget ran out mid-thought - no answer.
    think_close = chr(60) + "/think" + chr(62)
    if think_close in text:
        text = text.split(think_close, 1)[1].strip()
    elif text.startswith(chr(60) + "think"):
        text = ""
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"raw": content}
    except json.JSONDecodeError:
        return {"raw": content}


def request_headers(step: str, entry: str, doc_id: str | None) -> dict[str, str]:
    """Headers every pipeline request carries.

    X-API-Key authenticates the step to the gateway's PPE policy filter
    (manifests/base/praxis/policy.yaml — keyless requests fail closed);
    X-Session-Id keys the per-document session that the sensitive entries
    taint. The X-Demo-* labels are what the trace relays record.
    """
    headers = {"Content-Type": "application/json", "X-Demo-Step": step,
               "X-Demo-Entry": entry,
               "X-API-Key": api_key_for_step(step)}
    if doc_id:
        headers["X-Demo-Doc"] = doc_id
        headers["X-Session-Id"] = doc_id
    return headers


class ReviewRun:
    def __init__(self, general_base: str, sensitive_base: str | None, concurrency: int,
                 governance: bool, clause_keys: list[str] | None = None,
                 timeout: float = 600.0, token: str | None = None) -> None:
        self.clause_keys = clause_keys or list(CLAUSE_KEYS)
        # When the trace UI is exposed through a Route it can require a
        # bearer token; the relay forwards the header, so the same token
        # works for every entry.
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        def client(base: str | None) -> httpx.AsyncClient | None:
            return (httpx.AsyncClient(base_url=base, timeout=timeout, headers=headers)
                    if base else None)

        self.general = client(general_base)
        self.sensitive = client(sensitive_base)
        self.concurrency = max(1, concurrency)
        self.sem = asyncio.Semaphore(self.concurrency)
        self.governance = governance
        self.records: list[dict] = []
        self.screened: dict[str, bool] = {}
        self.screen_stats = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}

    async def close(self) -> None:
        for client in (self.general, self.sensitive):
            if client:
                await client.aclose()

    def entry_for_sensitivity(self, screened_sensitive: bool
                              ) -> tuple[httpx.AsyncClient, str]:
        """Pick the data-classification entry, never a pool.

        The pipeline's PII screen is the only routing decision made here: a
        document with personal data goes to the sensitive listener, everything
        else to the general listener. Praxis reads X-Demo-Step on the request
        and chooses the pool (specialist, CPU or GPU) in config.
        """
        if screened_sensitive and self.governance and self.sensitive:
            return self.sensitive, "sensitive"
        return self.general, "general"

    def note_screen(self, screened: bool, truth: bool | None) -> None:
        """Score the screen only where an independent label exists.

        CUAD has no PII annotations; recording its ``None`` truth as a
        negative would manufacture false positives.
        """
        if truth is None:
            return
        key = ("tp" if screened == truth and truth else "fp" if screened else
               "tn" if not truth else "fn")
        self.screen_stats[key] += 1

    async def call(
        self,
        client: httpx.AsyncClient,
        entry: str,
        step: str,
        body: dict,
        doc_id: str | None,
        true_complexity: str,
        true_sensitive: bool,
    ) -> dict:
        headers = request_headers(step, entry, doc_id)
        record: dict[str, Any] = {
            "step": step,
            "doc_id": doc_id,
            "entry": entry,
            "true_complexity": true_complexity,
            "true_sensitive": true_sensitive,
            "request_body": body,
        }
        started = time.monotonic()
        async with self.sem:
            try:
                resp = await client.post("/v1/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as exc:
                record.update(
                    {
                        "outcome": "transport_error",
                        "error": str(exc),
                        "latency_ms": round((time.monotonic() - started) * 1000),
                        "content": {},
                        "headers": {},
                    }
                )
                self.records.append(record)
                return record
        record["latency_ms"] = round((time.monotonic() - started) * 1000)
        captured = {h: resp.headers.get(h) for h in CAPTURE_HEADERS if resp.headers.get(h) is not None}
        record["headers"] = captured
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {})
            except Exception:
                err = {"message": resp.text[:200]}
            record.update({"outcome": f"http_{resp.status_code}", "error": err.get("message", "")})
        else:
            data = resp.json()
            route = captured.get("x-demo-route") or ""
            label = (captured.get("x-demo-classification") or "").upper()
            # Degradation is judged against the classifier's own decision: a
            # COMPLEX/REASONING label served by a non-GPU pool is degraded. A
            # failover response carries no label (praxis starts the failover
            # step with empty headers), so there the step's own tier decides:
            # only REASONING work counts as degraded when it lands on CPU -
            # MEDIUM/COMPLEX work on CPU is the classifier's intended routing.
            if label:
                gpu_tier = label in ("COMPLEX", "REASONING")
            else:
                gpu_tier = true_complexity == "REASONING"
            mismatch = gpu_tier and route != "" and "gpu" not in route.lower()
            fallback = captured.get("x-praxis-fallback") == "true" or mismatch
            record.update(
                {
                    "outcome": "fallback" if fallback else "served",
                    # What answered, as the server named it. Each route names
                    # one endpoint now, but response_model remains the only
                    # per-stage evidence of which checkpoint did the work.
                    "response_model": data.get("model"),
                    "content": parse_content(
                        data.get("choices", [{}])[0].get("message", {}).get("content")
                    ),
                }
            )
        self.records.append(record)
        return record

    async def review_document(self, doc: dict, doc_text: str) -> dict:
        doc_id = doc["doc_id"]
        screened = detect_sensitivity(doc_text)
        self.screened[doc_id] = screened
        self.note_screen(screened, doc.get("sensitive"))
        client, entry = self.entry_for_sensitivity(screened)
        await self.call(client, entry, "classify", classify_body(doc_text), doc_id, "SIMPLE", screened)
        clauses: dict[str, str | None] = {}
        for clause in self.clause_keys:
            step = f"extract:{clause}"
            client, entry = self.entry_for_sensitivity(screened)
            display = clause.replace("_", " ")
            rec = await self.call(client, entry, step, extract_body(doc_text, display),
                                  doc_id, "SIMPLE", screened)
            content = rec.get("content", {})
            clauses[clause] = content.get("text") if content.get("found") else None
        client, entry = self.entry_for_sensitivity(screened)
        await self.call(client, entry, "risk", risk_body(clauses, screened), doc_id, "COMPLEX", screened)
        return clauses

    async def run(self, docs: list[dict], doc_texts: dict[str, str]) -> None:
        t0 = time.time()
        # Documents are independent; the semaphore in call() bounds the
        # number of in-flight requests at self.concurrency. Awaiting them one
        # at a time made --concurrency cosmetic.
        await asyncio.gather(
            *(self.review_document(doc, doc_texts[doc["doc_id"]]) for doc in docs)
        )

        doc_lines = []
        report_rows: list[dict] = []
        any_sensitive = False
        for doc in docs:
            doc_records = [r for r in self.records if r["doc_id"] == doc["doc_id"]]
            risk = "unknown"
            risk_recs = [r for r in doc_records if r["step"] == "risk"]
            if risk_recs:
                risk = risk_recs[-1].get("content", {}).get("risk", "unknown")
            clause_recs = {
                r["step"].split(":", 1)[1]: r for r in doc_records if r["step"].startswith("extract:")
            }
            law = "unknown"
            law_rec = clause_recs.get("governing_law")
            if law_rec:
                content = law_rec.get("content", {})
                law = content.get("text", "unknown") if content.get("found") else "missing"
            dp_rec = clause_recs.get("data_protection")
            dp_present = False
            if dp_rec:
                dp_present = bool(dp_rec.get("content", {}).get("found"))
            # The report step must be chosen by what the screen saw, not by
            # the manifest label; the label is ground truth used for scoring.
            screened = self.screened.get(doc["doc_id"], False)
            any_sensitive = any_sensitive or screened
            row = {
                "ref": doc["doc_id"],
                "type": doc["type"],
                "risk": risk,
                "governing_law": law,
                "data_protection": dp_present,
                "sensitive": screened,
            }
            report_rows.append(row)
            doc_lines.append(json.dumps(row, ensure_ascii=False))
        prefixed = [f"- {line}" for line in doc_lines]
        client, entry = self.entry_for_sensitivity(any_sensitive)
        has_data_protection = DATA_PROTECTION_KEY in self.clause_keys
        report_rec = await self.call(
            client, entry, "report",
            report_body(prefixed, len(docs), has_data_protection), None,
            "REASONING", any_sensitive,
        )
        # The counts are a rollup of the rows above, not a model judgment.
        # Keep the model's answer for synthesis, but make the audited report
        # fields deterministic so a long batch cannot lose rows in the prompt.
        if report_rec.get("outcome") == "served":
            content = report_rec.get("content")
            if not isinstance(content, dict):
                content = {}
            content.update(report_rollup(report_rows, has_data_protection))
            report_rec["content"] = content
            report_rec["report_rollup"] = True
        self.wall_s = time.time() - t0


def load_documents(data_dir: str | Path, limit: int | None = None,
                   doc_ids: list[str] | None = None
                   ) -> tuple[list[dict], dict[str, str], list[str]]:
    """Load documents, optionally by explicit id.

    ``limit`` keeps the historical behaviour of preferring the manifest's
    golden set, which is right for a statistical run but means ``--docs 1``
    always picks the first golden document - and on the synthetic set that
    document is not sensitive. A demo that has to show the sensitive path
    picks its documents explicitly instead; ``doc_ids`` wins over ``limit``.
    """
    data = Path(data_dir)
    manifest = json.loads((data / "ground_truth.json").read_text())
    clause_keys = manifest.get("clause_keys") or CLAUSE_KEYS
    docs = manifest["documents"]
    if doc_ids:
        wanted = set(doc_ids)
        known = {d["doc_id"] for d in docs}
        missing = sorted(wanted - known)
        if missing:
            raise SystemExit(f"doc ids not in {data_dir}: {missing}")
        docs = [d for d in docs if d["doc_id"] in wanted]
    elif limit:
        golden = set(manifest.get("golden", []))
        docs = [d for d in docs if d["doc_id"] in golden] + [
            d for d in docs if d["doc_id"] not in golden
        ]
        docs = docs[:limit]
    texts = {}
    for doc in docs:
        texts[doc["doc_id"]] = (data / "documents" / f"{doc['doc_id']}.txt").read_text()
    return docs, texts, clause_keys


def summarise(records: list[dict], screen_stats: dict) -> dict:
    steps = len(records)
    by_route: dict[str, int] = {}
    by_label: dict[str, int] = {}
    by_entry: dict[str, int] = {}
    by_model: dict[str, int] = {}
    stage_models: dict[str, dict[str, int]] = {}
    stage_routes: dict[str, dict[str, int]] = {}
    judgment_steps = 0
    judgment_on_gpu = 0
    fallback = 0
    errors = 0
    classifier_us: list[int] = []
    gpu_busy_ms = 0.0
    cpu_busy_ms = 0.0
    sensitive_steps = 0
    sensitive_on_approved = 0
    sensitive_violations = 0
    for r in records:
        h = r.get("headers", {})
        route = h.get("x-demo-route")
        step = r.get("step", "")
        stage = step.split(":", 1)[0] or "unknown"
        if route:
            by_route[route] = by_route.get(route, 0) + 1
            per_stage_routes = stage_routes.setdefault(stage, {})
            per_stage_routes[route] = per_stage_routes.get(route, 0) + 1
            if stage in JUDGMENT_STEPS:
                judgment_steps += 1
                if route in GPU_ROUTES:
                    judgment_on_gpu += 1
        label = h.get("x-demo-classification")
        if label:
            by_label[label] = by_label.get(label, 0) + 1
        by_entry[r.get("entry", "general")] = by_entry.get(r.get("entry", "general"), 0) + 1
        model = r.get("response_model")
        if model:
            by_model[model] = by_model.get(model, 0) + 1
            per_stage = stage_models.setdefault(stage, {})
            per_stage[model] = per_stage.get(model, 0) + 1
        if r.get("outcome") == "fallback" or h.get("x-praxis-fallback") == "true":
            fallback += 1
        if r.get("outcome", "").startswith(("http_", "transport")):
            errors += 1
        if h.get("x-llm-d-sc-latency-us"):
            try:
                classifier_us.append(int(h["x-llm-d-sc-latency-us"]))
            except ValueError:
                pass
        if h.get("x-busy-ms"):
            try:
                busy = float(h["x-busy-ms"])
                if route in GPU_ROUTES:
                    gpu_busy_ms += busy
                else:
                    cpu_busy_ms += busy
            except ValueError:
                pass
        if r.get("true_sensitive"):
            sensitive_steps += 1
            if route in APPROVED_ROUTES:
                sensitive_on_approved += 1
            elif route:
                sensitive_violations += 1
    latencies = [r.get("latency_ms", 0) for r in records]
    return {
        "steps": steps,
        "routes": by_route,
        "labels": by_label,
        "entries": by_entry,
        # What each response said answered it. Older runs predate this field
        # and report an empty map rather than a guess.
        "models": by_model,
        "stage_models": stage_models,
        "stage_routes": stage_routes,
        # Risk/report steps and how many reached a GPU pool. Pool choice is
        # praxis's, so this is measured from the route header, not from the
        # entry the pipeline chose.
        "judgment_steps": judgment_steps,
        "judgment_on_gpu": judgment_on_gpu,
        "fallback": fallback,
        "errors": errors,
        "screen": screen_stats,
        "sensitive_steps": sensitive_steps,
        "sensitive_on_approved": sensitive_on_approved,
        "sensitive_violations": sensitive_violations,
        "classifier_latency_us_avg": int(sum(classifier_us) / len(classifier_us)) if classifier_us else None,
        "gpu_busy_ms": round(gpu_busy_ms),
        "cpu_busy_ms": round(cpu_busy_ms),
        "latency_ms_avg": int(sum(latencies) / max(len(latencies), 1)),
        "latency_ms_p95": sorted(latencies)[int(0.95 * len(latencies))] if latencies else 0,
    }


async def run_arm(
    gateway_base: str,
    sensitive_base: str | None,
    data_dir: str,
    out_dir: str,
    arm: str,
    docs_limit: int | None,
    concurrency: int,
    sample_metrics: str | None = None,
    timeout: float = 600.0,
    token: str | None = None,
    model: str | None = None,
    environment: str | None = None,
    cluster: str | None = None,
    pools: dict | None = None,
    doc_ids: list[str] | None = None,
    run_kind: str = "batch",
) -> dict:
    arm = CONFIG_ALIASES.get(str(arm).strip().lower(), arm)
    mode = CONFIG_MODES.get(arm, arm)
    governance = arm in GOVERNED_CONFIGS
    if governance and not sensitive_base:
        raise SystemExit("config hybrid needs --sensitive-gateway (the sensitive entry URL)")
    docs, texts, clause_keys = load_documents(data_dir, docs_limit, doc_ids)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    sampler = None
    metrics: dict | None = None
    if sample_metrics:
        from ..evalkit.sampler import MetricsSampler

        metric_pools = dict(kv.split("=", 1) for kv in sample_metrics.split(",")
                            if "=" in kv)
        sampler = MetricsSampler({k: v.rstrip("/") + "/metrics"
                                  for k, v in metric_pools.items()})
        sampler.start()

    run = ReviewRun(gateway_base, sensitive_base, concurrency, governance, clause_keys,
                    timeout=timeout, token=token)
    await run.run(docs, texts)
    await run.close()

    if sampler is not None:
        try:
            metrics = sampler.stop_and_summary()
            (out / "metrics.json").write_text(json.dumps(metrics, indent=1))
        except Exception as exc:  # metrics must never kill the run
            metrics = None
            print(f"WARN: metrics sampling failed: {exc}")

    summary = {
        "arm": arm,
        "config": CONFIG_NAMES.get(arm, arm),
        "mode": mode,
        "governance": governance,
        "docs": len(docs),
        "model": model,
        "concurrency": run.concurrency,
        "timeout_s": timeout,
        "started_at": started_at,
        "wall_s": round(run.wall_s, 2),
        # Whether the praxis policy entries are active. Kept under the old
        # key for existing score/report code: in the rebuilt stack both
        # judgment classes are routed by X-Demo-Step inside the single
        # general/sensitive pair, not by separate listeners.
        "judgment_split": governance,
        "sensitive_judgment_split": governance,
        **summarise(run.records, run.screen_stats),
        # Routes and labels say which pool answered; they cannot say what was
        # in the pool. Recording the deploy-time mapping with the run keeps
        # "direct vs through-stack" answerable months later.
        **run_provenance(environment, cluster, pools),
        # "batch" is a measured config arm; the MCP facade and ad-hoc smoke
        # checks post with a different kind so they cannot displace the matrix
        # row in the trace UI's "latest per config" table.
        "run_kind": run_kind,
    }
    if metrics:
        gpu_s = gpu_device_seconds(metrics, pools)
        summary["gpu_device_active_s"] = round(gpu_s, 2)
        summary["gpu_s_per_doc"] = round(gpu_s / max(len(docs), 1), 3)
    (out / "results.json").write_text(
        json.dumps({"summary": summary, "records": run.records}, indent=1)
    )
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(base_url=gateway_base, timeout=15.0,
                                     headers=headers) as client:
            await client.post("/_trace/api/runs", json=summary)
    except httpx.HTTPError:
        pass
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="contract-review pipeline")
    parser.add_argument("--gateway", required=True, help="general entry base URL (trace-ui ingress)")
    parser.add_argument("--sensitive-gateway", default=None,
                        help="sensitive entry base URL; required for the hybrid config")
    parser.add_argument("--data", default="data/synthetic")
    parser.add_argument("--out", required=True)
    parser.add_argument("--config", default=None,
                        help="routing policy, not hardware: one-pool, small-first, "
                             "round-robin or hybrid (old all-gpu/gpu-small accepted)")
    # Legacy alias. Kept so old scripts and recorded commands still run, but
    # hidden from help: the policy names are the supported interface.
    parser.add_argument("--arm", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--docs", type=int, default=None,
                        help="run the first N documents; with --doc-ids unset, "
                             "golden documents are preferred (so --docs 1 is not "
                             "the sensitive demo document)")
    parser.add_argument("--doc-ids", default=None,
                        help="comma-separated document ids, e.g. "
                             "C-2026-0001,C-2026-0003; overrides --docs")
    parser.add_argument("--model", default=None,
                        help="checkpoint label recorded in the summary, e.g. "
                             "Qwen/Qwen3.5-35B-A3B; a gateway alias alone does not "
                             "identify the weights")
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="per-request client timeout (seconds); thinking "
                             "mode generates many extra tokens and needs more")
    parser.add_argument("--sample-metrics", default=None,
                        help="pool=metrics-url pairs, comma-separated (e.g. "
                             "vllm-cpu=http://127.0.0.1:18081); scraped during the "
                             "run, summary written to metrics.json for the scorer")
    parser.add_argument("--token", default=os.environ.get("TRACE_API_TOKEN") or None,
                        help="bearer token for a trace UI protected by "
                             "TRACE_API_TOKEN (default: that env var)")
    parser.add_argument("--environment", default=None,
                        help="where this ran, e.g. g5.12xlarge-instance or sno; "
                             "defaults to HYBRID_ENVIRONMENT")
    parser.add_argument("--cluster", default=None,
                        help="cluster identity, e.g. sno-rh-dev; defaults to HYBRID_CLUSTER")
    parser.add_argument("--pools", default=None,
                        help="route → {model, hardware, deployment} JSON, or @path; "
                             "the harness cannot discover what sits behind a route")
    args = parser.parse_args(argv)
    chosen = args.config or args.arm
    if not chosen:
        parser.error("--config is required; choose one-pool, small-first, "
                     "round-robin or hybrid")
    try:
        config = resolve_config(chosen)
    except ValueError as exc:
        parser.error(str(exc))
    doc_ids = ([d.strip() for d in args.doc_ids.split(",") if d.strip()]
               if args.doc_ids else None)
    summary = asyncio.run(
        run_arm(args.gateway, args.sensitive_gateway, args.data, args.out, config,
                args.docs, args.concurrency, args.sample_metrics, args.timeout,
                args.token, args.model, environment=args.environment,
                cluster=args.cluster, pools=parse_pools(args.pools),
                doc_ids=doc_ids)
    )
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
