# Open-source reuse: what we adopted, what we refused, and how to measure it

Companion to [FINDINGS.md §16](../FINDINGS.md#16-open-source-reuse-audit-and-adapters-2026-11-05).
This page is the operator's view: where each adapter lives, how to turn it
on, and what number it can move.

The short version: **the commodity mechanics are now available from upstream;
the governance, routing and scoring decisions stay here.** Every adapter is
opt-in, an explicitly selected backend fails with an install/URL message when
absent, `auto` modes fall back and record which backend ran, and the measured
default path is unchanged (179 tests green).

## 1. Decision rule

A component was accepted only if it owns a mechanic we would otherwise
maintain, and only behind a flag/extra with the existing behaviour as the
default. A component was rejected as a *replacement* if it would:

- move a number without a paired A/B (PII screen, chunker, metric),
- bypass the praxis governance path (batch-to-pool, direct simulator),
- replace the audit trail instead of exporting alongside it, or
- take over a domain decision (pipeline prompts, rubric, scoring, composite
  routing, cache policy).

## 2. Audit and disposition

| area | hand-rolled before | upstream candidate | disposition |
|---|---|---|---|
| PII screen | 12 regexes in `classifier.py` | Presidio; llm-d-sc taxonomy | adapter; regex default |
| span scoring | containment in `score.py` | nervaluate | additive views; containment primary |
| significance | exact McNemar in `tools/mcnemar.py` | statsmodels | backend switch; exact fallback |
| ranking metrics | recall@k/MRR in `retrieval/evaluate.py` | ranx | NDCG/MAP backend; internal fallback |
| ingestion/chunking | builtin paragraph chunker | docling `HybridChunker` | adapter; builtin default |
| vector index | none (in-memory cosine) | LanceDB, bm25s | `PortfolioIndex`; memory default |
| semantic cache | in-memory dict/list | RedisVL + Redis | backend switch; policy stays ours |
| traces | SQLite + JSONL export | OpenTelemetry, Langfuse/Phoenix | OTel export additive; SQLite authoritative |
| RAG quality | none | RAGAS | optional judge metrics; lexical screen default |
| fake pool | `fakepool/server.py` | llm-d-inference-sim | proxy adapter; keep fake for cost/corruption |
| batch transport | Python runner only | llm-d-batch-gateway | client for `/v1/files`+`/v1/batches` |
| load generation | `tools/gpu_bench.py` | `vllm bench serve`, GuideLLM | command builder; pin versions |
| structured output | prompt asks for JSON | vLLM structured outputs | schema helper; off by default |

## 3. Defaults and activation

### PII: Presidio and the llm-d-sc taxonomy

`src/hybrid_demo/pii.py` keeps the original regex list and returns
character-offset `Finding`s.

```bash
cd demo
uv sync --extra pii
python -m spacy download en_core_web_lg   # Presidio's NER model
HYBRID_PII_DETECTOR=presidio uv run hybrid-review ...   # or set for one run
```

- `regex` (default): byte-identical routing to the recorded arms.
- `presidio`: NER + checksum recognisers. It will flag people, companies,
  addresses and identifiers; the sensitive share will rise. There is no CUAD
  PII label, so do not quote precision/recall from CUAD.
- `taxonomy`: expects a small HTTP bridge in front of llm-d-sc's gRPC
  taxonomy. Contract: `POST <url> {"text": "..."}` →
  `{"labels": [...]}` or `{"findings": [{start,end,entity}]}`; set
  `HYBRID_PII_TAXONOMY_URL`.
- `composite`: OR of the above, skipping unavailable backends.

`classifier.detect_sensitivity` now delegates to `pii.is_sensitive`, so the
pipeline screen and the specialist cache stay in sync. A detector failure
fails **closed** (treated as sensitive), preserving the governance invariant.

Comparison tool:

```bash
uv run python tools/compare_pii.py --data data/synthetic --detectors regex,presidio
uv run python tools/compare_pii.py --cuad data/cuad/cuad-main/data.zip --limit 20
```

### Span scoring: exact, containment, partial, nervaluate

`src/hybrid_demo/evalkit/spans.py`.

```bash
uv sync --extra spans
uv run pytest tests/test_spans_stats.py -q
```

`extraction_scores` keeps `precision/recall/f1` as containment and adds:

```json
{
  "match_metric": "containment",
  "by_mode": {
    "exact":       {"precision": ..., "recall": ..., "f1": ...},
    "containment": {"precision": ..., "recall": ..., "f1": ...},
    "partial":     {"precision": ..., "recall": ..., "f1": ...}
  },
  "offset": {"strict": {...}, "exact": {...}, "partial": {...}, "ent_type": {...}},
  "offset_backend": "nervaluate"
}
```

Offsets require document text; `build_report` loads
`data/<set>/documents/*.txt`. A prediction that cannot be located (a
paraphrase) is scored by the text modes but is a miss in nervaluate. That is
the intended reading; containment remains the headline number so old reports
stay comparable.

### Significance: statsmodels

`src/hybrid_demo/evalkit/stats.py` keeps `exact_mcnemar` as the fallback.

```bash
uv sync --extra stats
uv run python tools/mcnemar.py run_a.json run_b.json --k 5 --backend auto
```

The output records `backend: "statsmodels" | "exact"`. The table construction
is `[[both, b], [c, neither]]`, exact test, no continuity correction.

### Ranking: ranx, NDCG, MAP

`src/hybrid_demo/retrieval/metrics.py`.

```bash
uv sync --extra ranking
uv run hybrid-retrieval-eval --model litillabs/litil-embed-0.6b \
  --metrics-backend ranx --query-prompt clause
```

The evaluator now reports `ndcg_at_10`, `map_at_10` and `ranking_backend`
alongside the historical `recall_at_k`/`mrr`. If ranx is absent, `auto`
uses the internal implementation and says so.

### Ingestion: docling `HybridChunker`

`src/hybrid_demo/retrieval/docling_chunking.py`.

```bash
uv sync --extra ingest
uv run hybrid-retrieval-eval --model litillabs/litil-embed-0.6b \
  --chunk-backend docling --tokenizer-model litillabs/litil-embed-0.6b
```

- Default is `builtin`; `auto` uses docling only when installed.
- Offsets are relative to the exported text (`export_to_text()`), which is
  what the pipeline quotes.
- A provenance charspan is used only when the slice corroborates the chunk
  text. Otherwise the chunk text is relocated by search, or anchored with a
  visible `relocated` count. Do not silently trust a merged chunk's first-item
  charspan (the D11 bug class).
- `merge_peers=True` and `max_tokens=400` are defaults; record them with the
  run.

### Portfolio index: LanceDB + BM25

`src/hybrid_demo/retrieval/index.py`.

```bash
uv run hybrid-index build --chunks chunks.jsonl --out runs/index \
  --model litillabs/litil-embed-0.6b --backend auto
uv run hybrid-index query --index runs/index --q "which contracts cap liability" \
  --model litillabs/litil-embed-0.6b --k 5 --alpha 0.5
```

- `memory` is the CI/default backend; `lancedb` is used when installed.
- Sparse search is internal BM25 (or `bm25s` when present); hybrid score is
  `alpha * dense + (1-alpha) * sparse` after min-max normalisation.
- Chunk text and char offsets are stored with each vector so a hit is
  quotable and checkable.

### Semantic cache: RedisVL + Redis

`src/hybrid_demo/specialist/cache.py`.

```bash
uv sync --extra cache
uv run hybrid-specialist --semantic-cache --cache-backend redis \
  --cache-redis-url redis://redis:6379
```

The wrapper, not RedisVL, owns the policy:

1. exact key lookup in Redis first;
2. sensitive context is neither stored nor served;
3. at most one similarity candidate, filtered to the same clause;
4. false-positive checks compare cached vs fresh answers.

RedisVL supplies the vector index/eviction. Redis 7.4 is RSALv2/SSPL and
Redis 8 adds an AGPLv3 option; RedisVL is Apache-2.0. If the server licence is
unacceptable, Valkey is the drop-in.

### Observability: OTel beside SQLite

`src/hybrid_demo/traceui/otel.py`.

```bash
TRACE_OTEL=jsonl TRACE_OTEL_JSONL=/data/otel-spans.jsonl uv run hybrid-traceui
TRACE_OTEL=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 \
  uv sync --extra otel && uv run hybrid-traceui
```

- SQLite stays the audit trail; export happens after the row is written and
  cannot break the relay.
- Attributes follow `gen_ai.*` where the convention exists, plus
  `app.routing.*`, `app.governance.*`, `app.hops.*`.
- Sensitive content is not exported. Prompt tokens come from
  `x-prompt-tokens` when present, otherwise an estimate; the span marks
  `gen_ai.usage.input_tokens_estimated`.
- JSONL is dependency-free and is what tests use. OTLP needs the `otel`
  extra; if the SDK/exporter is missing the span falls back to JSONL and the
  error is visible via `otel.status()`/stderr.

Langfuse (MIT) is the preferred dashboard if one is published; Phoenix is
ELv2, a licensing caveat. Neither replaces the relay.

### RAG quality: lexical screen + RAGAS

`src/hybrid_demo/evalkit/rag.py`.

```python
from hybrid_demo.evalkit.rag import evaluate, evaluate_records, ragas_scores

evaluate(answer, contexts, reference=None)
evaluate_records(records)          # deterministic, no judge
ragas_scores(samples, llm=judge)   # optional, judge-dependent
```

- `groundedness`: fraction of answer content tokens found in evidence.
- `context_precision`: fraction of evidence sentences sharing a content token
  with the answer.
- `context_recall`: reference-token coverage (when a reference exists).

These are screens, not RAGAS. RAGAS's faithfulness/relevancy numbers are only
comparable across runs with the same judge model; record the judge.

### Simulation: llm-d-inference-sim proxy

`src/hybrid_demo/fakepool/llmd_sim.py`.

```bash
# terminal 1
llm-d-inference-sim --model Semantic-Qwen --port 8001 --mode random
# terminal 2
uv run hybrid-pool-sim --upstream http://127.0.0.1:8001 \
  --pool vllm-cpu --port 8000
# point VLLM_CPU_UPSTREAM=http://127.0.0.1:8000 at the stack
```

- Pass-through of `/v1/chat/completions`, `/v1/completions`, `/v1/models`,
  `/metrics`, `/health`; streaming is relayed.
- Adds `X-Pool`, `X-Replica`, `X-Simulator`, `X-Proxy-Busy-Ms`,
  `X-Proxy-Ttft-Ms`.
- Does **not** invent `X-Busy-Ms`: proxy wall time is not device time, and the
  cost report sums `X-Busy-Ms`. Keep `hybrid-pool` for the measured cost/
  corruption arms; use this adapter for transport, streaming, latency and
  OpenAI-compatibility work. `LLMD_SIM_DEVICE_TIME_HEADERS=1` is an explicit
  opt-in that must be labelled in the report.
- The simulator's failure injection is HTTP/API errors, not the demo's
  deterministic complexity-tier answer corruption.

### Batch: llm-d-batch-gateway

`src/hybrid_demo/pipeline/batch.py`.

```bash
uv run hybrid-batch --gateway http://localhost:8080 \
  --requests requests.jsonl --out results.jsonl
```

Input lines are `{"custom_id": "...", "body": {...}}` (or full
`method/url/body` lines). The client uploads `/v1/files`, creates
`/v1/batches`, polls, and downloads `/v1/files/{id}/content`.

Point it at the **praxis ingress**, never a pool. The gateway's native path
routes to the llm-d router and would bypass the governance/audit path. Batch
is a transport for portfolio-scale submission; the Python runner remains the
measurement harness.

### Load generation: vLLM bench / GuideLLM

`src/hybrid_demo/bench.py`.

```bash
# current vLLM (no --output-json anymore)
uv run hybrid-bench --backend vllm --base-url http://localhost:8080 \
  --model Semantic-Qwen --num-prompts 200 --request-rate 4 \
  --output results/vllm.json --dry-run

# current GuideLLM (v0.7+ CLI)
uv run hybrid-bench --backend guidellm --base-url http://localhost:8080 \
  --model Semantic-Qwen --request-rate 4 --max-seconds 120 --dry-run
```

The wrapper builds commands and does not parse their reports, so their format
can change without breaking us. Pin the CLI version in a measured run and
record it with the result.

### Structured outputs

`src/hybrid_demo/pipeline/schemas.py`; schemas for classify/extract/risk/
report.

```bash
HYBRID_STRUCTURED_OUTPUTS=vllm uv run hybrid-review ...
HYBRID_STRUCTURED_OUTPUTS=openai uv run hybrid-review ...
HYBRID_STRUCTURED_OUTPUTS=legacy uv run hybrid-review ...   # guided_json
```

Default is off, so request bodies and recorded results are unchanged.
Constraining decoding can change answer length/quality; A/B it.

## 4. Measurement protocol for any swap

1. Pin the upstream version and record it (container tag or package version).
2. Run the default and the swap on the same run IDs/documents, same seed,
   same concurrency, same prompts.
3. Compare the primary metric **and** the mechanism metric: sensitive share
   for PII, chunk-offset parity for docling, NDCG/MRR for ranking, hit rate +
   FP for cache, device seconds for serving.
4. Keep the default report columns; put the new number beside them.
5. If the swap changes routing or cost, re-run the four arms; do not mix
   adapter and non-adapter rows in one headline.

## 5. Licensing notes

| project | licence | note |
|---|---|---|
| Presidio | MIT | spaCy model has its own licence; check the model |
| nervaluate | MIT | small, pure Python |
| statsmodels | BSD-3-Clause | |
| ranx | MIT | check its numba/numpy pins against the env |
| docling / docling-core | MIT | PDF parsing stack pulls its own deps |
| LanceDB | Apache-2.0 | embedded store |
| RedisVL | Apache-2.0 | server licence is separate (see above) |
| Redis server 7.4+ | RSALv2/SSPL; Redis 8 AGPLv3 option | Valkey is BSD-3 |
| OpenTelemetry | Apache-2.0 | SDK/exporter |
| RAGAS | Apache-2.0 | judge/model terms are separate |
| llm-d-inference-sim | Apache-2.0 | |
| llm-d-batch-gateway | Apache-2.0 | |
| vLLM / GuideLLM | Apache-2.0 | |
| Langfuse | MIT (core) | check enterprise features |
| Phoenix | ELv2 | not OSI-approved; licensing caveat |

## 6. Verification status

- 179 tests pass; 33 new adapter tests cover defaults, opt-in wire formats,
  offset relocation, JSONL export, mock batch transport, metric definitions
  and the cache factory.
- Base environment: `presidio`, `docling`, `ranx`, `redisvl`, `statsmodels`
  and the OTel SDK are **not** installed; the adapters raise explicit
  install/URL errors and the defaults still run.
- Not yet measured: Presidio on a labelled sample, docling offset parity on
  the D11 set, ranx NDCG/MAP on CUAD, multi-replica Redis hit rate, RAGAS with
  the demo judge, live OTLP, GPU throughput from the new wrappers.
