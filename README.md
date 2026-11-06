# Contract portfolio intelligence on private, hybrid CPU + GPU inference

A legal-ops demo, fully measured: a corpus of contracts is screened for
personal data, clause extraction runs on a fine-tuned specialist (CPU,
milliseconds per clause, **cannot hallucinate — it returns spans of the
source document**), risk judgment and cross-document synthesis escalate to a
Qwen3.5-35B-A3B MoE, and **every request is routed and audited by
[Praxis](https://github.com/praxis-proxy/ai)** — no classifier in the
request path (removed 2026-11-05; FINDINGS §15.5). Documents
containing personal data are pinned to approved in-cluster pools only and
**fail closed** rather than spill. Batch volume runs on CPU; GPU is for
governed work, interactive drill-down and burst. Quality is scored against
expert ground truth ([CUAD](https://www.atticusprojectai.org/cuad)), routes
and device-time are recorded per request, and the gateway's own database is
the audit trail.

The product direction is **portfolio intelligence**: retrieval-first,
evidence-first — docling ingestion on CPU, legal embedders tiered by
measured regime (within-doc 0.6B on CPU, cross-doc portfolio queries on
the 8B — FINDINGS §11), specialist span extraction, MoE
synthesis over the
retrieved evidence, governance at every hop. See
[FINDINGS.md](FINDINGS.md) §11 for the design and §3/§10 for the measured
results that justify it.

Built on [RMalone8/llm-d-sc-demo](https://github.com/RMalone8/llm-d-sc-demo)
(Praxis gateway + trace UI), extended with the specialist pool, the CUAD
pipeline, the measurement harness and the OpenShell sandbox. **Read
"What is measured vs simulated" before quoting any number.**

**New to the repo?** Read [docs/design.md](docs/design.md): the system map
and naming model (routing-policy names, route names, models, and actual
hardware are five different things), the routing/governance design and why
there is no classifier, the upstream-composition policy, the
semantic-routing path, and the open decisions.

## Status: built vs designed

| capability | state |
|---|---|
| Praxis routing, governance, fail-closed, audit export | **built, verified on 3 stacks** |
| CUAD review pipeline (extraction → risk → report), scored vs ground truth | **built** |
| CUAD specialist pool (RoBERTa-large QA, composite with forwarding) | **built** |
| Semantic cache in the specialist (V5 ordering rules, zero-FP threshold) | **built, measured** |
| Four-arm measurement (A/B/C/D) + model-variant comparison on OCP | **built** |
| Trace UI (waterfall, arms panel, dashboard, audit) | **built** |
| Test suite | **196 tests green** (`cd demo && uv run pytest`) |
| Contract-step taxonomy + evaluation harness | **candidate, not measured** — evaluate before routing ([design §5](docs/design.md)) |
| Praxis 0.9.0 / ai-gateway 0.9.0 gateway rebuild | **built and published** — `quay.io/mtahhan/gci-praxis:v0.9.0`, base manifests switched ([docs](deploy/images/praxis-llm-d-sc/README.md)) |
| Retrieval evaluation (chunking + recall@k/MRR vs annotation spans) | **run, both regimes — ordering inverts within-doc, restores cross-doc (FINDINGS §11)** |
| OSS adapters (Presidio, nervaluate, ranx, docling, RedisVL, OTel, RAGAS, llm-d sim/batch/bench) | **built as opt-in swaps, default path unchanged** ([design §4](docs/design.md)) |
| docling ingestion + PII screen at ingest | adapter built; default remains builtin/regex, not yet measured on the corpus (§11, FINDINGS §16) |
| hybrid-bench concurrency sweep, thinking-mode A/B, V2 tool-calling | wrapper built; sweep open (FINDINGS §9) |

## How it fits together

The product pipeline — what a batch of contracts flows through:

```
docling ingestion (CPU)  ─>  PII screen at ingest                 [designed]
        │   structure-aware chunks, char offsets preserved
        v
litil-embed-0.6b retrieval (CPU tier)                             [measured, §11]
        v
CUAD specialist span extraction (CPU, ms/clause)                  [built]
        │   span JSON + evidence chunks
        v
Qwen3.5-35B-A3B synthesis: risk judgment, cross-doc report        [built]
```

Every hop goes through the same governed request path — the part that is
built, governed and measured end-to-end today:

![As-built architecture: clients (pipeline, sandboxed agent) → trace-ui / praxis gateway → pools; the dashed green boundary marks the approved set, inside which sensitive data is processed](docs/architecture-as-built.svg)

- **Sensitivity is a constraint; batch is CPU-first.** The general listener
  pins single-document work to the CPU pool (the measured behaviour: the
  classifier used to label 1,200 of 1,201 steps MEDIUM, FINDINGS §15.5, so
  the pin reproduces the recorded routing without it). The sensitive
  listener — fed by the PII screen — pins to CPU too, but its failover pool
  contains *only* the approved GPU backend, so when both are down a request
  **fails closed** (explicit 502, verified in the traces). General work
  degrades to any pool with a visible signal (the trace's "fell back"
  badge).
- **Praxis reaches the pools only through the trace-ui relays** — every
  request recorded with route, per-hop timings; SQLite is the audit
  trail, exported as `audit.jsonl`.
- **The same praxis configs** (`manifests/base/praxis/`) drive the Linux
  stack and OpenShift, so routing is byte-identical on both.
- **Arms** (measurement baselines, swapped live): **A** all-GPU large model,
  **B** all-GPU small model, **C** naive hybrid round-robin, **D** the hybrid
  policy — CPU-first pinned general/sensitive entries plus pinned judgment
  entries for risk and report (GPU), with fail-closed sensitive failover.
  The pinned judgment entries are an implemented policy, not yet a measured
  ROSA result; the measured D run predates them (FINDINGS §15.5).

## What runs where

| Environment | What runs | How |
|---|---|---|
| **Linux host (podman/docker)** | Real Praxis + trace-ui, simulated vLLM backends | `scripts/linux/up.sh` |
| **g5.12xlarge (stopped, AMI kept)** | Real vLLM: 1.5B CPU, 7B/1.5B GPU — the measured GPU-vs-CPU story | warm-restart the AMI |
| **OpenShift (rhgaudi3s2 SNO)** — validation | Real vLLM pools, all CPU-served (1.5B / 7B / 9B / 35B-A3B MoE / specialist), full platform validation | `deploy/openshift/*` overlays |
| **ROSA HCP — demo target (measured, torn down 2026-11-04)** | x8i.24xlarge CPU (48 cores, AMX) + g5.12xlarge (4× A10G, TP=4) in one AZ, explicit OMP binding, per-node gp3 caches | `deploy/openshift/rosa` |
| **Dev machine (tests)** | Fake pools + trace-ui + a praxis stub that parses the real configs | `cd demo && uv run pytest` |

The simulated backends speak the OpenAI subset, report device-time, simulate
prefix caching and inject deterministic tiered errors, so routing,
governance, failover and measurement run on a CPU-only instance and in CI.
**Quality and dollar numbers from them are simulation** — point the stack at
real vLLM pools (Linux compose profile `real`, the g5, or OpenShift) for
measured numbers.

## Optional open-source integrations

The measured default path is unchanged: the built-in regex screen, built-in
chunker, containment scoring, SQLite trace store and dependency-free exact
McNemar all still run with the base install. Each item below is an **opt-in
swap**, because a component swap changes a number and must be A/B tested, not
silently adopted. The full audit, licences and measurement caveats are in
[docs/design.md §4](docs/design.md).

| upstream | module / flag | default | why it is a swap |
|---|---|---|---|
| Presidio | `hybrid_demo.pii`, `HYBRID_PII_DETECTOR=presidio` | regex | NER raises the sensitive share; no CUAD PII labels to score it |
| nervaluate | `hybrid_demo.evalkit.spans` | containment | adds SemEval offset metrics; paraphrases still fail offsets |
| statsmodels | `hybrid_demo.evalkit.stats` | exact binomial | same test, standard implementation/backend label |
| ranx | `hybrid_demo.retrieval.metrics`, `--metrics-backend` | internal | NDCG/MAP implementation can differ at ties |
| docling `HybridChunker` | `hybrid_demo.retrieval.docling_chunking`, `--chunk-backend docling` | builtin | chunk boundaries move; offsets are regression-checked |
| LanceDB + BM25 | `hybrid_demo.retrieval.index`, `hybrid-index` | memory | cross-doc ranking changes with the backend |
| RedisVL + Redis | `--cache-backend redis` | in-memory | cache hits change cost/latency; policy stays ours |
| OpenTelemetry | `TRACE_OTEL=jsonl` or `otlp` | SQLite only | export is additive; no content for sensitive entries |
| RAGAS | `hybrid_demo.evalkit.rag` | lexical screen | judge-dependent numbers are not comparable across judges |
| llm-d-inference-sim | `hybrid-pool-sim` | `hybrid-pool` | simulator covers transport/latency, not tiered answer corruption or device time |
| llm-d-batch-gateway | `hybrid-batch` | Python runner | batch must target the praxis ingress, never a pool directly |
| vLLM bench / GuideLLM | `hybrid-bench` | `tools/gpu_bench.py` | load generator CLI churns; pin versions in a measured run |

Quick checks that need no model or GPU:

```bash
cd demo
uv run python -m hybrid_demo.pii                      # detector availability
uv run python tools/compare_pii.py --data data/synthetic --detectors regex
uv run python -m hybrid_demo.bench --backend vllm \
  --base-url http://localhost:8080 --model Semantic-Qwen --dry-run
uv run python -m hybrid_demo.fakepool.llmd_sim \
  --upstream http://localhost:8001 --pool vllm-cpu --port 8000 --print-simulator-cmd
```

## Linux runbook (podman or docker)

Prerequisites: podman or docker with a compose provider, `uv`, `curl`. On a
podman host without compose:

```bash
python3 -m venv ~/pv && ~/pv/bin/pip install podman-compose
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
export COMPOSE="$HOME/pv/bin/podman-compose"    # or "docker compose"
export PATH="$HOME/.local/bin:$PATH"

scripts/linux/up.sh        # build + start the stack (hybrid config), wait for readiness
scripts/linux/smoke.sh     # 18 assertions: routing, fallback, fail-closed, governance, audit,
                           # policy auth (keys fail closed) + shadow PII observe lines
DOCS=40 scripts/linux/arms.sh   # all four routing configs on the same documents + report
scripts/linux/demo.sh      # choreographed on-stage run (pauses between acts)
scripts/linux/down.sh      # stop (-v also deletes the trace database)
```

Outputs: `runs/report.md` (+ `.json`, `.html`), per-config directories such as
`runs/hybrid/`, and the live dashboard on http://127.0.0.1:8080. Pull the plug
yourself: `podman stop hybrid-demo_fake-gpu_1`.

Real contracts instead of synthetic: `scripts/data/fetch-cuad.sh`, convert
with `hybrid_demo.pipeline.cuad` (CUAD v1, CC BY 4.0 — verify before use;
use `--split test` for specialist comparisons, contamination discipline).
CUAD runs need the real-model stack. Then:
`DOCS=40 DATA=data/cuad-test scripts/linux/arms.sh`.

## OpenShift runbook

Prerequisites: OpenShift ≥ 4.19 (Landlock) with RHOAI/KServe optional, a GPU
node if you want one, cluster-admin, `oc`, `helm`, the `openshell` CLI.

```bash
oc login ...
./scripts/00-prereqs.sh            # checks + Agent Sandbox CRD/controller
./scripts/01-install-openshell.sh  # OpenShell gateway (Helm)
./scripts/02-apply.sh              # praxis, trace-ui, netpols
./scripts/03-create-sandbox.sh     # OpenClaw sandbox, egress: praxis only
```

Model pools are plain Deployments applied by CPU-model overlays — swap
variants with `scripts/cluster/model-comparison.sh {base|moe|9b|specialist|specialist-moe}`
(each applies `deploy/openshift/cpu-real-*`, warms praxis, runs config hybrid
on the CUAD test split into `runs/cuad-test/hybrid-<variant>` with metrics
sampling). The overlays carry the platform fixes from FINDINGS §7 (cpu
limits, writable cache volumes, /dev/shm sizing, model-cache PVC for small
models, emptyDir for the MoE — read it before changing anything).

The measured pools are Guaranteed QoS and assume the NTO CPU-isolation
profile in [`deploy/openshift/cpu-isolation/`](deploy/openshift/cpu-isolation/):
SMT siblings offlined so one CPU request is one physical core, and
`single-numa-node` placement. That profile is **generated for the node it
runs on**, not portable: the SNO here is a Xeon 6960P with six 24-core NUMA
nodes, so each 24-core 7B pool needs a whole node. On any other node the CPU
numbering, NUMA boundaries and pool-to-node fit differ — read `lscpu` first
and regenerate the three lists (commands in that README). The Linux stack and
the g5 run no NTO profile at all: use their numbers for routing and quality,
and on the g5 for device-time, never for CPU-latency comparisons against the
isolated OpenShift runs.

Apply and verify it before a measurement series — the README there has the
numbering check, the per-node CPU budget, the server-side dry-run command,
and the opt-in transparent-hugepages experiment. Without the profile the
pods still run, but their cpuset is not deterministic and their latency is
not comparable with the recorded runs.

The gateway image is published by this repo: upstream `ai` has no
`llm_d_sc` filter, so the repo publishes its own build of the fork —
`quay.io/mtahhan/gci-praxis:v0.9.0` (Praxis 0.7.5 / ai-gateway 0.9.0,
FINDINGS §12) — via the `publish-images` workflow
([deploy/images/praxis-llm-d-sc/](deploy/images/praxis-llm-d-sc/README.md)).
The filter is unused by the shipped configs (no classifier in the request
path) but stays in the image for the contract-step taxonomy experiment. The
classifier (`quay.io/mtahhan/gci-llm-d-sc:main-e660fff`) and its mirrored
modelcar were published once (2026-11-05) and stay in the registry for that
experiment, but nothing in the default stack deploys them; the workflow
jobs were removed with the classifier. The llm-d-sc Deployment and the
classifier-routed arm B config are preserved in git history (commit
`aa080e9`).

Then:

```bash
scripts/cluster/arms.sh            # measurement arms through the real pools
oc -n openshell-demo port-forward svc/trace-ui 8080:8080   # dashboard
scripts/demo/pull-the-plug.sh [--restore]   # scale a GPU pool to zero mid-run
scripts/demo/blocked-action.sh     # sandbox egress denials (after 03 + port-forward)
```

Routing configs are swapped by **policy name** (`hybrid`, `all-gpu`, …) —
the one catalog of names, aliases and manifests is
`demo/src/hybrid_demo/configs.json`, with `scripts/configs.sh` as its shell
adapter. `scripts/cluster/arms.sh` is the measured path: `CONFIGS="all-gpu
hybrid"` selects, the default runs the full matrix, and `hybrid-run --config
hybrid` is the CLI form. A manual swap points the deployment at the manifest
behind the name — the files keep their historical `arm-*` filenames for
recorded-evidence compatibility, so `all-gpu` is
`oc -n openshell-demo set env deploy/praxis PRAXIS_CONFIG=/etc/praxis/arm-a.yaml`
— then `oc -n openshell-demo rollout status deploy/praxis`. NetworkPolicies
restrict the pools to the trace-ui relays (the V15 enforcement layer). The
OpenClaw sandbox keeps its egress policy: praxis (read-write) and read-only
api.github.com; everything else denied and logged (`openshell logs`).

The trace UI has no Route in the base install — the dashboard holds contract
questions and responses. To publish it, set a token and opt in:

```bash
oc -n openshell-demo set env deploy/trace-ui TRACE_API_TOKEN=<token>
oc apply -k manifests/overlays/trace-ui-route
```

The relays reach trace-ui through the Service either way. The audit trail is
SQLite on the trace-ui PVC, exported as plaintext `audit.jsonl`; raw traces
are a rolling window of 250 (`TRACE_MAX_TRACES`), so a governance check is
only complete when the window covers the run — per-run summaries in the
`runs` table are the durable record. Treat the PVC and exports as sensitive,
and apply your own retention beyond the demo's.

## Measurement design

Same documents, same replay, same scoring for every arm; the report
compares:

- **Quality vs ground truth**: clause extraction P/R/F1 (exact spans vs
  expert annotations), risk accuracy (vs majority-class baseline), report
  accuracy, documents fully correct.
- **Classifier (historical)**: the recorded arms carry predicted labels vs
  step types (real llm-d-sc labels labelled all contract-analysis steps
  MEDIUM, the cross-doc report REASONING — reported honestly, FINDINGS §4,
  §15.5). New runs have no classifier and no label column.
- **Routing and governance**: CPU-served share; sensitive steps on approved
  pools; violations. Target for the hybrid config: **zero**, verified from the
  trace UI's own records, independently of the pipeline.
- **Cost**: GPU-seconds and CPU core-seconds per document, priced as a sweep
  (cloud list vs owned at 25/50/75% utilisation) — never a single $ figure.
- **Resilience**: degraded (fell-back) answers, fail-closed errors, recovery.

## Headline results

All numbers, protocols and caveats in [FINDINGS.md](FINDINGS.md) — read it
before quoting any of these:

- **Governance**: the hybrid config = 0 sensitive violations on every stack,
  every dataset — including 0/247 sensitive steps on the 200-document ROSA
  run. Ungoverned controls leak: ROSA all-gpu/gpu-small 247/247, round-robin
  122/247; earlier SNO/CUAD all-gpu/gpu-small 25, round-robin 12–24. This is a
  containment result — routing, not PII ground truth (FINDINGS §2).
- **Quality (CUAD held-out test split, 19 unique contracts)**: 1.5B F1 0.242
  (recall 1.0, precision ~0.15 — always answers, usually wrong: the worst
  failure shape for legal work); 35B-A3B MoE F1 0.690, risk 0.632 with real
  discrimination, identical on CPU and 4× A10G (same weights). The 7B was
  never run on the test split, so the old mixed-split comparison is retired.
  The retrieval-first specialist reaches F1 0.800 — a local direct run, not
  through-stack.
- **Direct parity (ROSA, 200 docs)**: the same 35B-A3B weights score
  identically on a bare CPU and a bare GPU (extract F1 1.0, risk 1.0; report
  0.746 vs 0.768), and all-CPU is 9.2× slower (2095 s vs 228 s). The only
  quality loss in the governed path is the specialist (F1 0.982), not the
  device; direct runs are ungoverned by construction (all 247 sensitive
  steps on one endpoint).
- **Cost (ROSA demo stack, 200 synthetic docs)**: the hybrid config used
  **0.036 GPU-s/doc vs the all-gpu config's 1.974 (54×)** — 7.3 vs 394.8
  GPU-seconds total — at extract F1 0.982 vs 1.0 and risk 0.995 vs 1.0. That
  GPU time is one cross-document report per run: the measured hybrid arm is a
  **CPU-first policy** (classify, extraction and risk on CPU; the portfolio
  report on GPU), not demonstrated semantic routing (FINDINGS §15.5). The
  earlier g5 validation
  measured 45× (0.21 vs 9.35). At the illustrative $1.10/GPU-hour that is
  ≈ $0.002 vs ≈ $0.12 of GPU time for the batch.
- **Retrieval (single runs)**: regime decides the winner. Within-document
  (the retrieve-then-extract regime) the MTEB(Law) ordering inverts — the
  0.6B litil beats the top-scoring 8B by ~10 points on R@1/MRR
  (McNemar p = 0.0075); cross-document (the portfolio-query regime) the 8B
  wins back (MRR 0.940 vs 0.829). Tiers by regime, not by leaderboard
  (FINDINGS §11).
- **Method > backbone** [external]: the CUAD leaderboard's method ladder
  (Standard 72.9 → RAG 79.3 → multi-agent 85.8) — the argument for the
  retrieval-first pivot.

## What is measured vs simulated

| Component | Linux stack | OpenShift (ROSA / g5) |
|---|---|---|
| Praxis routing, failover, health checks | real | real |
| trace UI / audit trail | real | real |
| Model pools | simulated (`hybrid-pool`) | real vLLM |
| Quality, device-time, dollars | simulated, labelled as such | measured (dollars an illustrative-rate sweep until V17) |

## Repository map

```
demo/src/hybrid_demo/
    pipeline/            generator, review pipeline, CUAD converter, batch client
    specialist/          composite specialist pool + semantic cache (V5)
    retrieval/           chunking, docling adapter, index, retrieval eval (§11)
    evalkit/             scorer, span/stats/RAG checks, live sampler, cost sweep
    fakepool/            simulated backends + llm-d-inference-sim adapter
    traceui/             recording relays, dashboard, audit, OTel export
    pii.py               pluggable PII detectors (regex default)
    bench.py             vLLM bench / GuideLLM command builder
demo/tools/              classifier probes, prompt calibration, validators,
                         paired-significance tests (mcnemar.py), PII compare
demo/tests/             193 tests incl. e2e against a praxis stub
manifests/               OpenShift base (pools, praxis, trace-ui, netpols)
manifests/base/          non-KServe base; base/praxis/ holds the four routing
                         configs, shared with the Linux stack
deploy/linux/            docker-compose / podman-compose stack
deploy/openshift/        cpu-real model-pool overlays: base | moe | moe-thinking |
                         9b | specialist | specialist-moe
scripts/linux/           up, down, smoke, arms, set_config, demo choreography
scripts/cluster/         model-comparison series (routes + port-forward fallback)
scripts/demo/            pull-the-plug, blocked-action
scripts/data/            CUAD fetch
runs/simulated/          simulated-stack evidence (four routing configs + reports)
runs/cuad-mixed/         mixed-split evidence (historical; quote test-split only)
runs/cuad-test/          held-out test-split evidence (hybrid variants) + comparisons/
runs/gpu-measured/       g5 measured-GPU evidence
runs/retrieval/          embedder evals, corrections, e2e runs (FINDINGS §11)
docs/                    design.md (the design doc), architecture + pipeline
                         diagrams (SVG), OCTO business-case doc (v0.3),
                         review guide
sandbox/policy.yaml      OpenShell sandbox egress policy (praxis + github RO)
FINDINGS.md              the measurement record — read before quoting numbers
```

## Manage the sandbox

`03-create-sandbox.sh` registers the gateway as plaintext at port 8080.
Port forward the gateway to manage your sandboxes:

```bash
oc -n openshell-demo port-forward svc/openshell 8080:8080
openshell sandbox list
openshell sandbox stop openclaw      # keeps the sandbox and its /sandbox PVC
openshell sandbox start openclaw
openshell policy set openclaw --policy sandbox/policy.yaml   # apply a changed policy
openshell sandbox exec openclaw -- ls /sandbox/.openclaw/workspace
openshell sandbox delete openclaw    # deletes the sandbox AND its /sandbox PVC
```

Re-run `./scripts/03-create-sandbox.sh` after a delete (no-op if it exists).

## Tear down

```bash
./scripts/99-teardown.sh --dry-run   # show what would be deleted
./scripts/99-teardown.sh             # asks first; --yes skips the prompt
scripts/linux/down.sh -v             # Linux stack, -v deletes traces too
```

## Known gaps

- docling ingestion, portfolio-scale retrieval and the hybrid-bench
  concurrency sweep are designed but not built (FINDINGS §9, §11).
- OpenShell's chart is experimental upstream; Kata is untested.
- The gateway runs plaintext HTTP behind port-forwards (add OIDC/mTLS
  before sharing). The trace UI can be published with an opt-in Route plus
  `TRACE_API_TOKEN`, and since the PPE integration the gateway path itself
  authenticates requests (per-step API keys, fail closed) — but the keys
  are fixed demo credentials checked into the repo, so that is attribution,
  not access control.
- The external llm-d-batch-gateway dispatches carry no `X-API-Key`, so the
  batch path through the gateway entries is denied by the policy layer
  until that component can present a key (docs/design.md §3.1).
- The sensitive entry is chosen by the pipeline's PII screen (a DLP-style
  gate). Since the PPE integration the gateway also runs an observe-only
  PII scanner over every request (`validator/gci-pii-observe`, pattern
  catalog mirroring the screen), measured against the pipeline's call by
  `hybrid-pii-agreement` — but it only observes, and the entry choice is
  still the client's. Moving that decision into the gateway — llm-d-sc's
  sensitivity taxonomy over gRPC, two-label routing in one praxis chain,
  or a promoted PPE rule — remains unvalidated (V19); the scanner's
  agreement number is the prerequisite evidence either way. Re-adding the
  classifier means git history, commit `aa080e9`. Network policies provide
  the enforcement layer either way (V15).
- rhgaudi3s2 was found emptied of our workloads on 2026-10-31 (FINDINGS
  §7); redeploy from the runbooks above before any cluster-dependent demo.
