# Decisions — how measurement changed the approach

This repo's method: build the smallest thing that answers a question,
measure it against ground truth, and let the number move the design. This
log records every consequential change of approach, in order, with the
measured result that triggered it and where the evidence lives. It is the
companion to [FINDINGS.md](FINDINGS.md) (the measurement record) and
[README.md](README.md) (the runbook); where they say *what*, this says
*why it changed*.

**D1 — Synthetic data retired for quality claims** (Oct 29–30)
Trigger: the 1.5B scored F1 1.000 on synthetic documents and 0.27 on real
contracts (7B: 0.965 → 0.761). Synthetic corpora flattered every model.
Change: CUAD (510 expert-annotated contracts) became the quality corpus;
synthetic documents survive only for governance/PII-screen drills.
Evidence: `runs/simulated/` vs `runs/cuad-mixed/`.

**D2 — Held-out test-split discipline** (Oct 31)
Trigger: mixed-split numbers read optimistic (MoE 0.80 → 0.712 on the
test split) — the mixed split contained train-split documents the CUAD
specialist checkpoints were fine-tuned on. Change: every externally
quoted number comes from `test.json` documents only; mixed-split numbers
are labelled historical. Evidence: `runs/cuad-test/`.

**D3 — The latency argument retired** (Oct 31)
Trigger: the CPU-served 35B-A3B MoE ran minutes-per-document and matched
or beat the GPU tier's quality (0.80 vs 0.761 mixed; test split 0.712).
Change: "contract review is batch" — GPU's claims were re-scoped to
interactive drill-down, burst absorption and governed work. The original
"latency justifies GPU" pitch is no longer made. Evidence: FINDINGS §5,
§10.

**D4 — Governance as the headline, not a feature** (Oct 29–31)
Trigger: arm D measured **0 sensitive violations on every stack, every
dataset**, while the naive arms leaked 12–25. Change: the two-entry
routing matrix (general/sensitive listeners, approved-pool set,
fail-closed) became the demo's core artefact; "sensitivity is a
constraint, complexity is a preference" became the thesis sentence.
Evidence: FINDINGS §2.

**D5 — The composite specialist pool** (Oct 30)
Trigger: the fine-tuned RoBERTa QA checkpoint cannot hallucinate by
construction and answers in milliseconds, but label-based routing cannot
express "extraction→specialist, judgment→MoE" (the classifier labels all
contract steps MEDIUM). Change: a composite CPU pool — QA answers
extraction prompts, everything else forwards to the general model —
explicitly a *config-level* composition waiting for finer classifier
labels upstream (see D12). Evidence: FINDINGS §3.1, §4.

**D6 — Embedder selection: leaderboard → measured corpus** (Oct 31 – Nov 2)
Trigger: the MTEB(Law) ordering **inverted** on our within-document task
(the 0.6B beat the top-scoring 8B, McNemar p = 0.0075, 125/157 vs 113/157
hit@5), then the 8B won
the cross-document regime, then the corrected re-measurement showed
everything ties at the top in both regimes. Change: candidate selection
became a funnel (leaderboard shortlist → local probe); the tier decision
moved from "quality" (measured out) to **cost / provenance / simplicity**.
Status: the final tier call is open — litil-0.6B for batch (tied, 13×
smaller), octen-8B or dinghy-8B for the interactive tier (tied; dinghy
adds the open-training-data provenance story). Evidence: `runs/retrieval/`,
FINDINGS §11.

**D7 — Product pivot: contract review → portfolio intelligence** (Nov 1)
Trigger: three measured facts aligned — the external method ladder
(Standard 72.9 → RAG 79.3 → multi-agent 85.8 on a fixed CUAD backbone:
*method moves numbers more than backbone size*), the specialist's
cannot-hallucinate/ms-per-clause profile, and retrieval measurements
showing regime-dependent embedder choice. Change: the product direction
became retrieval-first, evidence-first portfolio intelligence (docling
ingestion with PII screening at ingest, tiered embeddings, specialist span
extraction, MoE synthesis over retrieved evidence). Evidence: FINDINGS
§11.

**D8 — Generative extraction → retrieve-then-extract** (Nov 2)
Trigger: end-to-end measurement — the specialist fed litil-retrieved
top-5 chunks scored **F1 0.800** vs the generative MoE's 0.712 (the 1.5B:
0.242 with recall 1.0 — always answers, usually wrong: the worst failure
shape for legal work). Zero retrieval misses, zero specialist misses at
the top-5 window. Change: retrieve-then-extract became the pipeline core
of the pivot. Evidence: `runs/retrieval/e2e-specialist*.json`, FINDINGS
§11.

**D9 — Semantic cache: swap point, not a rewrite** (Oct 31)
Trigger: the stand-in embedding (character-trigram hash) could not
separate near-duplicate templates — the zero-FP operating point (0.98)
served only exact repeats. Change: the cache shipped with the ordering
rules and an explicit embedder swap point; litil-0.6b is the measured
swap-in candidate. Evidence: FINDINGS §4.1.

**D10 — Measurement-path hardening** (Nov 2)
Trigger: three distinct infrastructure failures contaminated runs in one
day — a port-forward tunnel death through the flapping API server (72/121
and 45/121 requests lost), the OpenShift route default 30s timeout
(11 × http_504 at exactly ~30.2s), and two lab DNS outages (initially
misread as node failure; pinned-IP checks proved the node healthy).
Change: the request path moved to Routes with a 600s timeout annotation;
metrics sampling stays on port-forwards by design (non-contaminating);
`/etc/hosts` entries immunise against DNS flaps; the lesson — *verify at
TCP with `--resolve` before declaring an outage* — is recorded in
FINDINGS §7. Evidence: FINDINGS §7, the contaminated arms' burst
patterns in `runs/cuad-test/`.

**D11 — The correction protocol** (Nov 2)
Trigger: a chunker offset bug made gold spans uncoverable on ~40% of
documents, silently depressing every retrieval number. Change: fix +
regression tests, then **re-measure everything affected and record the
correction openly** — old values superseded in place, correction note in
the record, ordering conclusions re-verified rather than assumed.
Evidence: FINDINGS §11 (correction block), `tests/test_chunking.py`.

**D12 — Upstream alignment (adopted direction)** (Nov 3)
Trigger: an upstream review of `llm-d-semantic-classifier` and the
`praxis-proxy` org. Change: see FINDINGS §12 — the llm-d-sc roadmap
answers our two open architecture items (multi-signal = two-label
routing; custom taxonomies = native specialist composition), and praxis
v0.8/v0.9 carry fixes we want (request-head classification, TTFT
metrics, in-flight per-model tracking). Re-checked 2026-11-03 against the
local checkouts: `praxis-proxy/praxis` is the framework (v0.7.5) and
`praxis-proxy/ai` the gateway (v0.9.0) — the versions in FINDINGS are the
gateway's; our fork image registry publishes only v0.7.0, so the upgrade is
a fork rebuild, not a tag bump — the configs' keys all still exist at
v0.9.0, and the fork's `llm_d_sc` filter is the work. llm-d-sc's new gRPC
health service is not in our pinned image, so the manifest keeps a TCP
readiness probe. Status: planned, not yet upgraded.

**D13 — CPU requests are physical cores; isolation is node-generated** (Nov 3)
Trigger: the pool requests were logical CPUs under `full-pcpus-only`.
That guarantees no other pod takes the sibling but still hands the pod
both sibling threads, so vLLM's OpenMP binding can put two threads on one
core; nothing pinned a pod to one NUMA node, so CPU latency depended on
placement. Change: every measured pool is Guaranteed QoS with an integer
request counted in **physical cores**, and measurement runs apply an NTO
PerformanceProfile that offlines the SMT siblings of the isolated cores
(the `offlined` list is explicit — NTO does not derive it) and enforces
`single-numa-node`. The profile is generated from the node it runs on,
not portable: the SNO is a Xeon 6960P with **six 24-core NUMA nodes**, so
each 24-core 7B pool takes a whole node (74 of 78 isolated cores in the
base layout, 78 of 78 for MoE/9B). The `full-pcpus-only` KubeletConfig is
kept only as a documented, non-comparable fallback; hugetlbfs pages are
not reserved (vLLM's CPU backend has no allocation path for them), and
transparent hugepages are an opt-in experiment. Evidence:
`deploy/openshift/cpu-isolation/`; the lists were checked against the
node's `lscpu` and all three manifests pass `oc apply --dry-run=server`
on 2026-11-03.

**D14 — Reuse the commodity parts, keep the measurement path** (Nov 5)
Trigger: an audit of the demo found several places where we maintain
mechanics that a mature upstream already owns (PII recognition, span
scoring, ranking metrics, PDF→chunk provenance, vector/semantic cache
storage, OTel export, load generation, the fake OpenAI server). At the
same time, the audit found the opposite failure mode in the obvious
"just adopt it" answers: replacing the trace relay, batching the Python
runner, or swapping the PII screen would move a measured number or
bypass governance. Change: implemented the upstream pieces as **opt-in
adapters** and left every measured default byte-identical. Adopted:
Presidio (`HYBRID_PII_DETECTOR`, default regex), nervaluate (extra span
views; containment stays primary), statsmodels (exact McNemar backend,
fallback kept), ranx (NDCG/MAP backend, internal fallback kept),
docling `HybridChunker` (`--chunk-backend`, default builtin, provenance
offsets corroborated before use), LanceDB + BM25 (`hybrid-index`;
memory default), RedisVL for similarity plus Redis for exact keys
(`--cache-backend redis`; policy stays in our wrapper), OpenTelemetry
GenAI-shaped JSONL/OTLP export alongside SQLite, RAGAS plus a
deterministic lexical groundedness screen, `llm-d-inference-sim` behind
a proxy that deliberately does **not** fabricate `X-Busy-Ms`, the
`llm-d-batch-gateway` `/v1/files`+`/v1/batches` client, and
`vllm bench serve`/GuideLLM command builders. Rejected as replacements:
the custom trace relay/dashboard, `pipeline/review.py` and
`evalkit/score.py` domain logic, the composite specialist, the
containment metric itself, and any batch path that points at a pool
instead of the praxis ingress. Status: adapters and tests are in tree;
no quality/cost number has been swapped yet. The next measured action
is an A/B per row, starting with Presidio on a labelled sample and
docling offsets on the D11 regression set. Evidence:
`docs/OPENSOURCE-REUSE.md`, FINDINGS §16, 179 tests green; explicitly
selected optional backends fail with an install/URL error, while `auto`
modes fall back and record which backend ran.

**D15 — Routing claims scoped to what the classifier measured** (Nov 4–5)
Trigger: the ROSA 200-document hybrid run — the only config with 0 sensitive
violations — ran as **all-CPU behind a governance gate**, not complexity
routing: llm-d-sc labelled 1,200 of 1,201 steps MEDIUM, and the specialist
composite's `--forward-to` pointed at the CPU MoE, so only the
cross-document report reached the GPU. The 54× GPU saving is the same fact
as the 99.9% CPU share: moving the ~600 forwarded generative calls to GPU
would give back most of the cost headline. Change (decided 2026-11-05):
**(A)** reword, no rerun — every description of the measured run now says
"CPU-first policy with the GPU reserved for cross-document synthesis",
never semantic/complexity routing (README, FINDINGS §15.1/§15.5, the OCTO
doc, the stage walkthrough, `runs/rosa-200/`); **(B)** stage-aware judgment
routing implemented — pinned praxis listeners (`:8086` general judgment →
`vllm-gpu`, `:8087` sensitive judgment → `vllm-gpu-approved`, no classifier,
no CPU failover, so a GPU outage fails closed), risk/report steps choose
the judgment entries, sensitivity still beats stage, summaries record
`stage_routes`/`judgment_split`, trace UI badges `JUDGE`/`SENS-JUDGE`.
Alongside: the contract-step taxonomy candidate
(`classifiers/contract-step.json` + the `hybrid-eval-contract-steps`
offline harness) and a reproducible Praxis 0.7.5 / ai-gateway 0.9.0
rebuild with the out-of-tree `llm_d_sc` filter
(`deploy/images/praxis-llm-d-sc/`) as the path to real semantic routing.
Status: A done; B built and tested but **not measured** — ROSA was torn
down 2026-11-04, and the ~6× estimate (~201 GPU calls, ~0.33 GPU-s/doc)
is an estimate, not a result; do not claim B numbers until it is rerun.
Warnings that survive the rerun: do not just repoint the specialist's
`--forward-to` (it forwards outside praxis and would bypass the approved
set); the pool relay's `X-Demo-Route` hides the composite's real backend;
and the 27 failed documents are a specialist termination-extraction issue,
not a routing issue. Evidence: `runs/rosa-200/` (summary, results,
classifier replay), FINDINGS §15.5, `docs/contract-step-routing.md`,
`deploy/images/praxis-llm-d-sc/`. The
full analysis with the option table and reproduction snippets is
preserved in git history (`docs/handoff-2026-11-04-rosa-routing.md` at
commit `8798da3`).

**D16 — CPU model choices checked against the vLLM xeon6 catalog** (Nov 4)
Trigger: a scan of recipes.vllm.ai (221 recipes, 40 `hw=xeon6` renderings)
found the deployed `Qwen3.5-35B-A3B` is the exact xeon6 recipe — but BF16
only, with **no CPU-quantized variant** (the GPTQ/FP8 checkpoints render
for XPU, not xeon6) — while two deployed models (`Qwen3.5-9B`,
`Qwen2.5-1.5B`) have no xeon6 recipe at all. Change: recommendation only,
**no manifest changed**. Keep `Qwen3.5-35B-A3B` (the cpu-real-moe flags
already match the rendered recipe; `VLLM_ENGINE_READY_TIMEOUT_S ≥ 1800`
stands); benchmark `openai/gpt-oss-20b` (3.6B active, native MXFP4,
~16 GB — fits a 16-core/64 GiB pool with real KV headroom) as the
CPU-synthesis challenger through the CUAD scorer; `Qwen3-30B-A3B` is the
conservative MoE fallback (no experimental Mamba path); if the dense
fallback is ever used for a quoted comparison, switch to `Qwen3-14B` or
say plainly the 9B is off-catalog. The CUAD specialist and the embedders
keep their custom serving — vLLM has no recipes for extractive QA or
those embedders. Warnings: the xeon6 profile is a **multi-socket** node,
so a recipe is a validated flag set, not a capacity claim for our
16-core NUMA-local pool slices — never cite the 40 recipes as "supported
on our CPU pool"; changing `--served-model-name` breaks the relays (all
CPU pools serve as `Semantic-Qwen`); cold start, not steady state, is the
rollout risk (seed caches, long readiness timeouts). Status: nothing
promoted — this repo's rule is measured, not catalog-based; ranked test
order: 35B-A3B (incumbent) → gpt-oss-20b → Qwen3-30B-A3B. Evidence: the
2026-11-04 catalog scan (recipes.vllm.ai, `hw=xeon6`);
`deploy/openshift/cpu-real*/`; FINDINGS §10–§11. The full review with
rendered commands and the test plan is preserved in git history
(`docs/handoff-2026-11-04-cpu-model-recipes.md` at commit `8798da3`).
