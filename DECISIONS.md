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
(the 0.6B beat the top-scoring 8B, McNemar p = 0.0001), then the 8B won
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
metrics, in-flight per-model tracking). Status: planned, not yet
upgraded.
