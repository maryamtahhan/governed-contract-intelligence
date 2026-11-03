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
from ..routes import APPROVED_ROUTES, GPU_ROUTES
from .generate import CLAUSE_KEYS
from .risk import DATA_PROTECTION_KEY, rubric_text

ARM_MODES = {
    "A": "all-gpu",
    "B": "gpu-split",
    "C": "round-robin",
    "D": "matrix",
}
ARMS_WITH_GOVERNANCE = {"D"}

# Thinking mode (the untested quality lever, FINDINGS §8): Qwen3.5 emits
# <think/>...</think/> blocks that consume the token budget before the
# answer, so each step's max_tokens scales when HYBRID_THINKING=1 and the
# parser strips think blocks before JSON extraction. Default unchanged.
THINKING = os.environ.get("HYBRID_THINKING", "") == "1"
STEP_TOKENS = {
    "classify": (200, 800),
    "extract": (300, 1500),
    "risk": (300, 1500),
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
    return {
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
    }


def extract_body(doc_text: str, clause: str) -> dict:
    return {
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
    }


def risk_body(clauses: dict, sensitive: bool) -> dict:
    clauses_json = json.dumps(
        {k: {"found": bool(v), "text": v} for k, v in clauses.items()}, ensure_ascii=False
    )
    has_data_protection = DATA_PROTECTION_KEY in clauses
    note = "This document contains personal data (see Schedule 3)." if sensitive else ""
    return {
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
    }


def report_body(doc_lines: list[str], count: int,
                has_data_protection: bool = True) -> dict:
    dp_request = (", count contracts missing a data-protection clause"
                  if has_data_protection else "")
    dp_field = (', "missing_data_protection": N' if has_data_protection else "")
    return {
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
    }


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


class ReviewRun:
    def __init__(self, general_base: str, sensitive_base: str | None, concurrency: int,
                 governance: bool, clause_keys: list[str] | None = None,
                 timeout: float = 600.0, token: str | None = None) -> None:
        self.clause_keys = clause_keys or list(CLAUSE_KEYS)
        # When the trace UI is exposed through a Route it can require a
        # bearer token; the relay forwards the header, so the same token
        # works for the ingress and the sensitive entry.
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.general = httpx.AsyncClient(base_url=general_base, timeout=timeout,
                                         headers=headers)
        self.sensitive = (
            httpx.AsyncClient(base_url=sensitive_base, timeout=timeout,
                              headers=headers)
            if sensitive_base
            else None
        )
        self.concurrency = max(1, concurrency)
        self.sem = asyncio.Semaphore(self.concurrency)
        self.governance = governance
        self.records: list[dict] = []
        self.screened: dict[str, bool] = {}
        self.screen_stats = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}

    async def close(self) -> None:
        await self.general.aclose()
        if self.sensitive:
            await self.sensitive.aclose()

    def entry_for(self, screened_sensitive: bool) -> httpx.AsyncClient:
        if screened_sensitive and self.governance and self.sensitive:
            return self.sensitive
        return self.general

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
        headers = {"Content-Type": "application/json", "X-Demo-Step": step}
        if doc_id:
            headers["X-Demo-Doc"] = doc_id
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
        client = self.entry_for(screened)
        entry = "sensitive" if client is not self.general else "general"
        await self.call(client, entry, "classify", classify_body(doc_text), doc_id, "SIMPLE", screened)
        clauses: dict[str, str | None] = {}
        for clause in self.clause_keys:
            display = clause.replace("_", " ")
            rec = await self.call(client, entry, f"extract:{clause}", extract_body(doc_text, display),
                                  doc_id, "SIMPLE", screened)
            content = rec.get("content", {})
            clauses[clause] = content.get("text") if content.get("found") else None
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
            doc_lines.append(
                json.dumps(
                    {
                        "ref": doc["doc_id"],
                        "type": doc["type"],
                        "risk": risk,
                        "governing_law": law,
                        "data_protection": dp_present,
                        "sensitive": screened,
                    },
                    ensure_ascii=False,
                )
            )
        prefixed = [f"- {line}" for line in doc_lines]
        client = self.entry_for(any_sensitive)
        entry = "sensitive" if client is not self.general else "general"
        has_data_protection = DATA_PROTECTION_KEY in self.clause_keys
        await self.call(client, entry, "report",
                        report_body(prefixed, len(docs), has_data_protection), None,
                        "REASONING", any_sensitive)
        self.wall_s = time.time() - t0


def load_documents(data_dir: str | Path, limit: int | None = None
                   ) -> tuple[list[dict], dict[str, str], list[str]]:
    data = Path(data_dir)
    manifest = json.loads((data / "ground_truth.json").read_text())
    clause_keys = manifest.get("clause_keys") or CLAUSE_KEYS
    docs = manifest["documents"]
    if limit:
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
        if route:
            by_route[route] = by_route.get(route, 0) + 1
        label = h.get("x-demo-classification")
        if label:
            by_label[label] = by_label.get(label, 0) + 1
        by_entry[r.get("entry", "general")] = by_entry.get(r.get("entry", "general"), 0) + 1
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
) -> dict:
    mode = ARM_MODES.get(arm, arm)
    governance = arm in ARMS_WITH_GOVERNANCE or arm == "matrix"
    if governance and not sensitive_base:
        raise SystemExit("arm D needs --sensitive-gateway (the sensitive entry URL)")
    docs, texts, clause_keys = load_documents(data_dir, docs_limit)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    sampler = None
    if sample_metrics:
        from ..evalkit.sampler import MetricsSampler

        pools = dict(kv.split("=", 1) for kv in sample_metrics.split(",") if "=" in kv)
        sampler = MetricsSampler({k: v.rstrip("/") + "/metrics" for k, v in pools.items()})
        sampler.start()

    run = ReviewRun(gateway_base, sensitive_base, concurrency, governance, clause_keys,
                    timeout=timeout, token=token)
    await run.run(docs, texts)
    await run.close()

    if sampler is not None:
        try:
            (out / "metrics.json").write_text(json.dumps(sampler.stop_and_summary(), indent=1))
        except Exception as exc:  # metrics must never kill the run
            print(f"WARN: metrics sampling failed: {exc}")

    summary = {
        "arm": arm,
        "mode": mode,
        "governance": governance,
        "docs": len(docs),
        "concurrency": run.concurrency,
        "wall_s": round(run.wall_s, 2),
        **summarise(run.records, run.screen_stats),
    }
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


def main() -> None:
    parser = argparse.ArgumentParser(description="contract-review pipeline")
    parser.add_argument("--gateway", required=True, help="general entry base URL (trace-ui ingress)")
    parser.add_argument("--sensitive-gateway", default=None,
                        help="sensitive entry base URL; required for arm D")
    parser.add_argument("--data", default="data/synthetic")
    parser.add_argument("--out", required=True)
    parser.add_argument("--arm", required=True, help="A, B, C, D")
    parser.add_argument("--docs", type=int, default=None)
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
    args = parser.parse_args()
    summary = asyncio.run(
        run_arm(args.gateway, args.sensitive_gateway, args.data, args.out, args.arm,
                args.docs, args.concurrency, args.sample_metrics, args.timeout,
                args.token)
    )
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
