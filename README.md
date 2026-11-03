# Contract portfolio intelligence on private, hybrid CPU + GPU inference

A legal-ops demo, fully measured: a corpus of contracts is screened for
personal data, clause extraction runs on a fine-tuned specialist (CPU,
milliseconds per clause, **cannot hallucinate — it returns spans of the
source document**), risk judgment and cross-document synthesis escalate to a
Qwen3.5-35B-A3B MoE, and **every request is classified, routed and audited
by [Praxis](https://github.com/praxis-proxy/ai)** with the
[llm-d-sc](https://quay.io/rmalone/llm-d-sc) semantic classifier. Documents
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
[DECISIONS.md](DECISIONS.md) for how measurement changed the approach,
[FINDINGS.md](FINDINGS.md) §11 for the design and §3/§10 for the measured
results that justify it.

Built on [RMalone8/llm-d-sc-demo](https://github.com/RMalone8/llm-d-sc-demo)
(Praxis + llm-d-sc + trace UI), extended with the specialist pool, the CUAD
pipeline, the measurement harness and the OpenShell sandbox. **Read
"What is measured vs simulated" before quoting any number.**

## Status: built vs designed

| capability | state |
|---|---|
| Praxis routing, governance, fail-closed, audit export | **built, verified on 3 stacks** |
| CUAD review pipeline (extraction → risk → report), scored vs ground truth | **built** |
| CUAD specialist pool (RoBERTa-large QA, composite with forwarding) | **built** |
| Semantic cache in the specialist (V5 ordering rules, zero-FP threshold) | **built, measured** |
| Four-arm measurement (A/B/C/D) + model-variant comparison on OCP | **built** |
| Trace UI (waterfall, arms panel, dashboard, audit) | **built** |
| Test suite | **58 tests green** (`cd demo && uv run pytest`) |
| Retrieval evaluation (chunking + recall@k/MRR vs annotation spans) | **run, both regimes — ordering inverts within-doc, restores cross-doc (FINDINGS §11)** |
| docling ingestion + PII screen at ingest | designed (§11) |
| hybrid-bench concurrency sweep, thinking-mode A/B, V2 tool-calling | open (FINDINGS §9) |

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

![As-built architecture: clients (pipeline, sandboxed agent) → trace-ui / praxis gateway → llm-d-sc classification → pools; the dashed green boundary marks the approved set, inside which sensitive data is processed](docs/architecture-as-built.svg)

- **Complexity is a preference, sensitivity is a constraint.** The general
  listener routes by the llm-d-sc complexity label (SIMPLE/MEDIUM → CPU,
  COMPLEX/REASONING → GPU). The sensitive listener — fed by the PII screen —
  routes the same labels but only to approved pools, and its failover pool
  contains *only* the approved GPU backend, so when that pool is down a
  REASONING request **fails closed** (explicit 502, verified in the traces).
  General REASONING work degrades to CPU with a visible signal (the trace's
  "fell back" badge).
- **Praxis reaches the pools only through the trace-ui relays** — every
  request recorded with label, route, per-hop timings; SQLite is the audit
  trail, exported as `audit.jsonl`.
- **The same four praxis configs** (`manifests/praxis/`) drive the Linux
  stack and OpenShift, so routing is byte-identical on both.
- **Arms** (measurement baselines, swapped live): **A** all-GPU large model,
  **B** all-GPU small model, **C** naive hybrid round-robin, **D** the full
  matrix — classifier + two entries + fail-closed sensitive failover.

## What runs where

| Environment | What runs | How |
|---|---|---|
| **Linux host (podman/docker)** | Real Praxis + llm-d-sc + trace-ui, simulated vLLM backends | `scripts/linux/up.sh` |
| **g5.12xlarge (stopped, AMI kept)** | Real vLLM: 1.5B CPU, 7B/1.5B GPU — the measured GPU-vs-CPU story | warm-restart the AMI |
| **OpenShift (rhgaudi3s2 SNO)** | Real vLLM pools, all CPU-served (1.5B / 7B / 9B / 35B-A3B MoE / specialist), full platform validation | `deploy/openshift/*` overlays |
| **Dev machine (tests)** | Fake pools + trace-ui + a praxis stub that parses the real configs | `cd demo && uv run pytest` |

The simulated backends speak the OpenAI subset, report device-time, simulate
prefix caching and inject deterministic tiered errors, so routing,
governance, failover and measurement run on a CPU-only instance and in CI.
**Quality and dollar numbers from them are simulation** — point the stack at
real vLLM pools (Linux compose profile `real`, the g5, or OpenShift) for
measured numbers.

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

scripts/linux/up.sh        # build + start the stack (arm D), wait for readiness
scripts/linux/smoke.sh     # 12 assertions: matrix, fallback, fail-closed, governance, audit
DOCS=40 scripts/linux/arms.sh   # arms A-D on the same documents + report
scripts/linux/demo.sh      # choreographed on-stage run (pauses between acts)
scripts/linux/down.sh      # stop (-v also deletes the trace database)
```

Outputs: `runs/report.md` (+ `.json`, `.html`), per-arm `runs/arm-*/`, and
the live dashboard on http://127.0.0.1:8080. Pull the plug yourself:
`podman stop hybrid-demo_fake-gpu_1`.

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
./scripts/02-apply.sh              # praxis, llm-d-sc, trace-ui, netpols
./scripts/03-create-sandbox.sh     # OpenClaw sandbox, egress: praxis only
```

Model pools are plain Deployments applied by CPU-model overlays — swap
variants with `scripts/cluster/model-comparison.sh {base|moe|9b|specialist|specialist-moe}`
(each applies `deploy/openshift/cpu-real-*`, warms praxis, runs arm D on the
CUAD test split into `runs/cuad-test/arm-D-<variant>` with metrics
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

Then:

```bash
scripts/cluster/arms.sh            # measurement arms through the real pools
oc -n openshell-demo port-forward svc/trace-ui 8080:8080   # dashboard
scripts/demo/pull-the-plug.sh [--restore]   # scale a GPU pool to zero mid-run
scripts/demo/blocked-action.sh     # sandbox egress denials (after 03 + port-forward)
```

Arms are swapped with `oc -n openshell-demo set env deploy/praxis
PRAXIS_CONFIG=/etc/praxis/arm-a.yaml && oc -n openshell-demo rollout status
deploy/praxis`. NetworkPolicies restrict the pools to the trace-ui relays
(the V15 enforcement layer). The OpenClaw sandbox keeps its egress policy:
praxis (read-write) and read-only api.github.com; everything else denied and
logged (`openshell logs`).

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
- **Classifier**: predicted labels vs step types (real llm-d-sc labels all
  contract-analysis steps MEDIUM, the cross-doc report REASONING — reported
  honestly, FINDINGS §4).
- **Routing and governance**: CPU-served share; sensitive steps on approved
  pools; violations. Target for arm D: **zero**, verified from the trace
  UI's own records, independently of the pipeline.
- **Cost**: GPU-seconds and CPU core-seconds per document, priced as a sweep
  (cloud list vs owned at 25/50/75% utilisation) — never a single $ figure.
- **Resilience**: degraded (fell-back) answers, fail-closed errors, recovery.

## Headline results

All numbers, protocols and caveats in [FINDINGS.md](FINDINGS.md) — read it
before quoting any of these:

- **Governance**: arm D = 0 sensitive violations (requests the screen
  labelled sensitive) on every stack, every dataset; A/B = 25, C = 12–24.
  This is a containment result — routing, not PII ground truth (FINDINGS §2).
- **Quality (CUAD held-out test split)**: 1.5B F1 0.242 (recall 1.0,
  precision ~0.15 — always answers, usually wrong: the worst failure shape
  for legal work); 35B-A3B MoE 0.712 (risk 0.65 with real discrimination).
  On the mixed split the MoE hit 0.80 vs the 7B-on-GPU's 0.761 — the CPU
  tier reached the GPU tier's quality.
- **Cost**: arm D used 0.21 GPU-s/doc vs arm A's 9.35 (45×) on the g5; a
  200-doc batch costs ≈ $0.01 of rented GPU time hybrid vs ≈ $0.57 all-GPU
  (calculated from measured rates).
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

| Component | Linux stack | OpenShift / g5 |
|---|---|---|
| Praxis routing, failover, health checks | real | real |
| llm-d-sc classification | real | real |
| trace UI / audit trail | real | real |
| Model pools | simulated (`hybrid-pool`) | real vLLM |
| Quality, device-time, dollars | simulated, labelled as such | measured (dollars an illustrative-rate sweep until V17) |

## Repository map

```
demo/src/hybrid_demo/
    pipeline/            generator, review pipeline, CUAD converter
    specialist/          composite specialist pool + semantic cache (V5)
    retrieval/           chunking, retrieval eval, end-to-end harness (§11)
    evalkit/             scorer, live metrics sampler, cost sweep
    fakepool/            simulated OpenAI-compatible backends
    traceui/             recording relays, dashboard, audit export
demo/tools/              classifier probes, prompt calibration, validators,
                         paired-significance tests (mcnemar.py)
demo/tests/              58 tests incl. e2e against a praxis stub
manifests/               OpenShift base (pools, llm-d-sc, praxis, trace-ui, netpols)
manifests/praxis/        the four routing configs, shared with the Linux stack
deploy/linux/            docker-compose / podman-compose stack
deploy/openshift/        cpu-real model-pool overlays: base | moe | moe-thinking |
                         9b | specialist | specialist-moe
scripts/linux/           up, down, smoke, arms, set_arm, demo choreography
scripts/cluster/         model-comparison series (routes + port-forward fallback)
scripts/demo/            pull-the-plug, blocked-action
scripts/data/            CUAD fetch
runs/simulated/          simulated-stack evidence (arms A–D + reports)
runs/cuad-mixed/         mixed-split evidence (historical; quote test-split only)
runs/cuad-test/          held-out test-split evidence (arm-D variants) + comparisons/
runs/gpu-measured/       g5 measured-GPU evidence
runs/retrieval/          embedder evals, corrections, e2e runs (FINDINGS §11)
docs/                    architecture (SVG/PNG), OCTO business-case doc (v0.2)
sandbox/policy.yaml      OpenShell sandbox egress policy (praxis + github RO)
FINDINGS.md              the measurement record — read before quoting numbers
DECISIONS.md             how measurement changed the approach, in order
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
  `TRACE_API_TOKEN`, but the gateway path itself has no auth.
- The sensitive entry is chosen by the pipeline's PII screen (a DLP-style
  gate); llm-d-sc's sensitivity modelcar exists but two-label routing in
  one praxis chain is unvalidated (V19). Network policies provide the
  enforcement layer either way (V15).
- rhgaudi3s2 was found emptied of our workloads on 2026-10-31 (FINDINGS
  §7); redeploy from the runbooks above before any cluster-dependent demo.
