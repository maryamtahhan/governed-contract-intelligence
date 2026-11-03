# Findings — Private, governed agents on hybrid CPU+GPU inference

Working record of everything measured and learned, newest sections last.
CUAD = Contract Understanding Atticus Dataset (510 expert-annotated
commercial contracts; CC BY 4.0, confirmed on the Atticus Project site,
accessed 2026-10-30). Every
number here is reproducible from the runbooks in the [README](README.md); the
reports referenced live under `runs*/` per stack. Claims are labelled
**[measured]** (our numbers, our replay), **[observed]** (single-run or
qualitative), or **[external]** (cited, not ours).

## 1. What was built

Three runnable instances of the same demo, byte-identical praxis routing
configs across all of them (`manifests/praxis/`):

| stack | where | pools | purpose |
|---|---|---|---|
| Linux/podman, simulated | any host (RHEL 10 CPU box used) | fake OpenAI-compatible pools with deterministic tiered error rates | CI, choreography, fast iteration |
| Linux/podman, real | g5.12xlarge (4× A10G, stopped, AMI kept) | real vLLM: 1.5B CPU, 7B/1.5B GPU | the measured GPU-vs-CPU story |
| OpenShift | rhgaudi3s2 SNO (4.22, 288 cores, 2.3TB, free) | real vLLM, all pools CPU-served (`accelerator: cpu` label) | full-platform validation: routing, governance, sandbox, measurement on OCP |

Plus the **CUAD specialist pool**: the paper's fine-tuned RoBERTa-large QA
checkpoint (354M, Zenodo) behind an OpenAI-compatible FastAPI wrapper,
deployed as a *composite* CPU pool — extraction prompts answered by the QA
model in one encoder pass, everything else forwarded to the general model
behind the same relay.

Architectural invariants kept throughout: every request goes
client → trace-ui → **praxis** (llm-d-sc label → pool) → pool; the trace UI's
SQLite is the audit trail; governance is verified from the gateway's own
records, not the pipeline's.

## 2. Governance and routing — the headline, replicated on every stack **[measured]**

Sensitive-document steps served outside the approved pool set
(`vllm-cpu`, `vllm-gpu-approved`), by arm:

| arm | violations | CPU-served share |
|---|---|---|
| A all-GPU | 25/25 | 0% |
| B gpu-split | 25/25 | 0% |
| C round-robin | 12–24 (varies by run) | ~50% |
| **D matrix** | **0 — on every stack, every dataset** | ~99% |

Also verified on all stacks: GPU-outage degrade with a visible signal
(trace "fell back" badge: REASONING label recorded on the failed GPU hop,
final route on CPU), **fail-closed** for SENSITIVE+REASONING when the
approved pool is down (explicit 502, never spilled — verified from traces),
recovery after pool restart, pool isolation during partial outage, live arm
swaps via praxis's config watcher, audit export (JSONL, one object per
trace), and the PII screen (precision 1.0, recall 1.0 on synthetic; 5–12 of
20 CUAD docs flagged, real contract text).

What "0 violations" does and does not mean: sensitivity is whatever the
pipeline's screen labels sensitive, and CUAD ships no PII ground truth
(`sensitive=None`). The count therefore proves that **no request the screen
called sensitive reached an unapproved pool** on any stack — a routing and
containment result, verified from the gateway's records independently of
the pipeline. It is not evidence that the CUAD corpus contains no PII, nor a
measure of screen accuracy on real contracts beyond the 20-document sample
above. Violations are counted per recorded trace; a request with no trace row
at all would not appear in the count, which is why the auto-routed paths are
exercised through the recording relays by construction.

## 3. Quality — what each tier of model is actually good at

### 3.1 Extraction is a copy task; real contracts are where it gets hard **[measured]**

Clause extraction (quote the clause), F1 vs expert ground truth:

| model | synthetic docs | CUAD (mixed split, 20 docs) | CUAD (held-out test split, 20 docs) |
|---|---|---|---|
| Qwen2.5-1.5B | 1.000 | **0.27** (P=0.156, R=1.0) | **0.242** |
| Qwen2.5-7B | 0.965 | 0.761 | not run |
| **Qwen3.5-35B-A3B (CPU)** | not evaluated on synthetic | **0.80** (P=0.714, R=0.909) | **0.712** (risk 0.65, 6/20 docs fully correct) |
| CUAD specialist (RoBERTa-large QA), full document | not applicable (extracts only from real text) | 0.60 on a 10-doc preview (P=0.43, R=1.0) | **0.780** [local direct run, §11] — through-stack arm contaminated, see §9 |
| **specialist on litil top-5 retrieved chunks** | not applicable | not run | **0.800** (P=0.667, R=1.000) [local direct run, §11] |

Test-split runs: arm D on OCP, 20 held-out documents, metrics sampled
(`runs/cuad-test/`). The mixed→test drop (MoE 0.80 → 0.712) is the
contamination discipline showing: the mixed split contained train-split
documents the specialist checkpoints were fine-tuned on, and optimistically
sampled docs generally. **Quote only test-split numbers externally from here
on.**

Findings:
- The 1.5B's failure shape on real contracts is the worst possible one for a
  legal workflow: recall 1.0, precision 0.156 — it always answers, usually
  quoting the wrong paragraph. Nothing flags itself as unreliable.
- The 7B drops 0.965 → 0.761 from synthetic to real: **synthetic data
  flattered every model**; only real-contract numbers should ever be quoted.
- The 35B-A3B MoE on CPU **beat the 7B on the GPU pool** on both extraction
  (0.80 vs 0.761) and risk (0.75 vs 0.6), at ~3B active params per token.
  Caveat stated plainly: part of that is model generation (Qwen3.5 vs
  Qwen2.5), not only size. The defensible claim: *the CPU tier reached the
  GPU tier's quality on this workload.*
- The specialist: recall 1.0 everywhere (finds every annotated clause), and
  **cannot hallucinate a quote by construction** (it returns spans of the
  input). Its "false positives" are largely annotation-coverage gaps, below.

### 3.2 CUAD's annotations have noise — the scorer under-credits everyone **[observed]**

Specialist answers marked wrong included e.g. "Either party may terminate
this Agreement without cause at any time upon thirty (30) days' written
notice" for Termination For Convenience, and "penalty shall not exceed 5% of
the total value" for Cap On Liability — real clauses the annotators did not
highlight. Implication: reported precision is a *lower bound* for all models;
comparisons between models remain fair (same annotations), absolute numbers
should be read against **[external]** SOTA ≈ 88.3 F1 on the CUAD test split.

### 3.3 Risk judgment scales with model class — and the rubric matters as much as the model **[measured]**

Rubric-following risk accuracy (exact match vs rule-derived truth):
- 1.5B: 0.1 on CUAD, 0.4 on synthetic = **at or below the majority-class
  baseline** (0.75 / 0.5). The 1.5B answers "low" (or near-constant)
  unconditionally: it is not applying the rubric at all.
- 7B: 0.6 (CUAD) / 0.7 (synthetic) — real but imperfect rubric engagement
  (missed the "medium" tier entirely on synthetic).
- 35B-A3B: 0.75 on CUAD **with discrimination** (16 high / 4 low — not
  parroting the majority class).
- Rubric design pitfall, stated for the audience: our CUAD risk rule keys on
  "no data-protection clause → high", and CUAD has no DP category, so nearly
  every doc is "high" and always-high is the optimal strategy. On synthetic
  (balanced rule) the metric behaves properly. Risk rubrics must be designed
  against the dataset or the number is meaningless.
- The risk rubric is deterministic given the extractions — a rule would score
  ~1.0. The LLM step measures *model judgment*, which is the thing you'd keep
  LLMs for only where rules can't be written.

### 3.4 Report accuracy inherits everything (propagation) **[measured]**

Cross-document report accuracy: 0.2–0.6 across arms, tracking upstream risk
and extraction errors plus its own synthesis errors. MoE arm: 0.54, 7/20 docs
fully correct.

### 3.5 External context **[external]**

- CUAD test-split SOTA, contract review: 88.3 F1
  (sota2.com/research/dataset/cuad, accessed 2026-10-30; third-party
  aggregator — treat as indicative, verify against primary papers before
  external publication).
- CUAD revision leaderboard: Qwen2.5-7B-Chat is the leading backbone
  (85.82 via multi-agent RCBSF, Apr 2026) — the same model family as our GPU
  pool. Method ladder on a fixed backbone: Standard 72.9 → CoT 75.2 → RAG
  79.3 → multi-agent 85.8. **Method moves CUAD numbers more than backbone
  size** — the strongest external argument for pipeline-shaped fixes
  (retrieve-then-quote) over model-shaped ones.
- Datasets by pipeline role (licenses must be verified per dataset; CUAD is
  CC BY 4.0, confirmed): LEDGAR → clause-type classification stage; CUAD →
  span extraction; ContractNLI → permission/obligation reasoning (the right
  future evaluation for the risk tier); MAUD → M&A deal points.

## 4. Classifier transfer (the plan's V14) **[measured]**

The real llm-d-sc complexity classifier labels **all contract-analysis steps
MEDIUM**; only the cross-document report prompts label REASONING. Consequences:
- Across-tier routing (MEDIUM→CPU, REASONING→GPU) works and is the demo's
  visible routing story.
- **Within-tier** composition (extraction→specialist, judgment→MoE) cannot be
  expressed in label-based routing today; it lives inside the composite pool.
  If the classifier gains finer labels (retrain), that composition moves up
  into praxis natively.
- Report prompts were tuned so the real classifier labels them REASONING
  (calibrated wording; probes in `demo/tools/`). The step-type truth is kept
  separately and the disagreement is reported, never hidden.

### 4.1 Semantic cache (the plan's V5) **[measured]**

Implemented in the specialist pool with the plan's ordering rules: exact-match
lookup is always safe; similarity serving sits after it and **never stores or
serves sensitive content** (the same PII screen as the pipeline gates both
directions - measured: 32/32 sensitive requests skipped, zero leaks).

Threshold sweep on the synthetic corpus (12 docs x 4 clauses, replayed twice,
FP = similarity-served answer disagreeing with a fresh computation):

| threshold | hit rate | similarity FP rate |
|---|---|---|
| 0.93 | 75.0% | 46.9% |
| 0.95 | 66.7% | 45.8% |
| 0.97 | 50.0% | 25.0% |
| **0.98** | **41.7%** | **0.0%** |
| 0.99 | 41.7% | 0.0% |

Findings:
- The zero-FP operating point is **0.98**, the default. At that point all
  hits are exact-match repeats (41.7% of the replayed workload) - free and
  safe.
- The stand-in embedding (256-dim character-trigram hash, chosen for zero
  dependencies) **cannot separate near-duplicate templates from
  different-template contracts**: below 0.98 it serves wrong clause text
  (cross-template pairs score 0.93-0.97), at 0.98+ similarity never fires.
  This is the measured justification for the plan's "a wrong cached answer
  is worse than a miss" warning. **The component is in place; making
  similarity caching demo-worthy requires swapping in a real embedding
  model** — litil-embed-0.6b, the best-measured embedder in §11, at a
  config-level swap point, not a redesign.

## 5. Cost and throughput **[measured]**

Device-time is measured two ways: exact counters (simulated pools) and
time-integral of in-flight gauges from live `/metrics` sampling (real pools;
batching caveat stated in every report). Pricing is a **sweep** (cloud list
vs owned at 25/50/75% utilisation), never a single figure — rates are
illustrative until a validated cost model exists (V17).

- g5 (real GPUs), synthetic 20 docs: **D used 0.21 GPU-s/doc vs A's 9.35
  (45× less)**; cloud-list prices favored A, owned-hardware ≥50% utilisation
  favored D — the plan's predicted break-even shape, measured.
- OCP (all pools CPU-served), CUAD 20 docs: D 1.46 pool-s/doc vs A 57.99.
- **The MoE's price is latency**: 35B-A3B on the shared CPU node ran ~25s
  avg / 60s p95 per request (51 min for a 20-doc arm). Same quality on GPU
  would be seconds. This is the "same quality, different speed" finding:
  the 35B-A3B is the first model we ran where CPU and GPU tiers can hold
  *identical quality* and the router chooses on speed/cost alone.
- The specialist's extraction costs milliseconds per clause (one encoder
  pass, no KV cache, no decode loop).

## 6. The composite/staged pipeline and the hybrid thesis

The staged arm (`specialist-moe`): specialist answers extraction (ms,
cannot hallucinate), the MoE receives only span JSON for risk — it never
ingests whole contracts for extraction. This arm exists to measure the
hypothesis; no results yet at time of writing (see section 9).

Guardrails that keep the hybrid thesis primary (agreed with stakeholders):
1. Every variant is a **routing configuration**, not a code path; the arms
   table stays about where steps run.
2. The on-stage moments remain praxis moments (label→pool waterfall,
   pull-the-plug, fail-closed, kill switch).
3. The reasoning tier stays on the GPU pool. "Same quality, different speed"
   is the *degradation* story (lose latency, not correctness), not the
   steady-state pitch.

The "just run a bigger model on GPU" objection, answered empirically:
- It is arm A — measured, and it lost on quality to the CPU MoE on this
  workload (generation caveat stated).
- The router agrees with the objector for the reasoning tier, which is where
  the GPU budget should go.
- The workload's ceiling is annotation noise and method, not parameters
  (SOTA ladder above).

## 7. Platform and infrastructure findings

OpenShift/rhgaudi bring-up (all fixes now baked into overlays):
- praxis sizes its thread pool from *node* CPU count (288) → OOM under a
  small memory limit; needs a CPU limit (we use 8) and ≥1Gi.
- The praxis Service exposes `80→8080`, not 8080: relays dialing
  `praxis:8080` get silent SYN drops (works in compose, dead via a Service
  VIP). Use the Service port.
- KServe storage-initializer could not reach HuggingFace on this cluster →
  pools became plain Deployments; the cpu-real overlays need no KServe.
- OpenShift random-UID serving: needs `HOME`/`HF_HOME`/`XDG_CACHE_HOME`
  pointed at a writable volume mounted at the mount root, Memory-backed
  /dev/shm (default 64Mi is too small), and memory headroom for the KV
  reservation check (1.5B fp32-upcast needs ~20Gi).
- First request after a praxis rollout can skip classification (cold gRPC to
  llm-d-sc → default cluster, no label). The arms script warms it; noted as
  a caveat for any deployment.
- praxis v0.7.0 does not emit `X-Praxis-Fallback` on proxied failovers; the
  visible degrade signal is the label/route mismatch, which the trace UI
  badges and the pipeline records (`outcome: fallback`).
- Pull-the-plug on OCP was a full pool cold start (~15 min: model
  re-download + compile; emptyDirs are per-pod). **Fixed**: all pools now
  mount a shared ReadWriteMany `model-cache` PVC (NFS storage class) at
  /cache — weights *and* vLLM compile caches survive pod recreation, and the
  same model is downloaded once for pools that share it. The HF cache is
  idempotent, so a restarted pod that finds the weights just boots (~2 min).
  No KServe needed for this benefit (we had left KServe on this cluster
  anyway — its storage-initializer cannot reach HuggingFace here); plain
  Deployments + a PVC-mounted cache are equivalent for small models (the
  1.5B pool downloads to it fine).
- **NFS + large-model downloads OOM via dirty-page accounting** (measured,
  three failed seed attempts): writing the MoE's 72 GB through NFS
  accumulates dirty pages inside the container's memory cgroup faster than
  the NFS server flushes, so the kernel OOM-kills the downloader at any
  memory limit — the process RSS is small; the page cache is not. The MoE
  pool therefore stays on an emptyDir (local disk, ~25 min re-download per
  pod recreation) and the rust frontend is disabled
  (`VLLM_USE_RUST_FRONTEND` unset — the Python frontend is the proven path
  here; the rust frontend's engine-startup deadline was the original crash
  trigger).
- Editing a bash script while a series runs it will corrupt the run (bash
  re-reads by byte offset). Restart cleanly; the guard is in the runbook.
- **SNO instability day (2026-11-02), recorded for the rerun decision**: an
  API-server blip (~12:11), CNI/multus pod-sandbox creation failures from
  ~10:30 that wedged the kserve controller in `ContainerCreating` (dead
  KServe webhook blocked all overlay applies; fixed by deleting the pod —
  a fresh pod UID gets a fresh sandbox), pods cluster-wide showing 8–13
  restarts, and `oc port-forward` tunnels dying mid-run — the last one
  contaminated both specialist arms (§9). Runbook fixes landed the same
  day: specialist image builds from a **minimal staged context** (the
  2026-10-31 build death was `--from-dir=demo` uploading ~10GB of local
  model caches), the Dockerfile downloads/unpacks its checkpoint with
  python (python:3.12-slim ships no curl/unzip), and the pool health
  check uses python3 for the same reason.
- **Local DNS outage, initially misread as node loss (~17:50, 2026-11-02)**:
  the lab router (192.168.1.1) stopped resolving the cluster's domain —
  NXDOMAIN for the API and route hostnames, ICMP loss at the node's LAN
  address. Pinned-IP checks proved the node and all services healthy
  (API `/healthz` 200, route `/_trace/healthz` 200 via
  `curl --resolve`); the fix is three `/etc/hosts` entries pointing the
  API and route hostnames at the node address. Lesson recorded: ICMP +
  DNS failure is not node failure — verify at TCP with `--resolve`
  before declaring an outage. Consequences for the rerun queue are
  unchanged: the route-based measurement path is validated (0 transport
  errors, 26.5-min arm wall vs 88 min through port-forwards) but needs
  the `haproxy.router.openshift.io/timeout` annotation (11 http_504s at
  exactly ~30.2s — the router default — on the one run that completed;
  fix already in the script); the specialist-moe arm needs one final
  clean rerun, the specialist arm its first, and the thinking-mode A/B
  (`moe-thinking` variant, implemented and unit-tested) is ready to go
  first on the warm MoE pool.
- **`oc start-build --dockerfile` is not supported on this server** (4.22):
  the flag errors out. Build a Dockerfile from a directory instead — patch
  the BuildConfig's `spec.strategy.dockerStrategy.dockerfilePath` and use
  `oc start-build <name> --from-dir=<dir>` (fix applied in
  `scripts/cluster/model-comparison.sh`; the specialist image bakes its
  checkpoint in at build time).
- **rhgaudi3s2 workloads were torn down**: the hybrid-demo namespace was
  found empty on 2026-10-31 (all pools, praxis, trace-ui, builds and the
  model-cache PVC gone) after the specialist series had already died with a
  lost API connection mid-build. Cause unknown — possibly cluster reclamation
  (free SNO). All evidence packs are local (`runs*/`); the stack is
  re-deployable from the runbooks in README (the overlays carry every fix
  above). The Gaudi finding below is unrelated and pre-dates this.
- **Gaudi hardware on rhgaudi is dead**: 8× Gaudi 3 present by DMI, but no
  kernel module, no `/dev/hl*`, driver DS idle, device plugin unschedulable
  (missing `habana.ai/driver.version` label); another team's Gaudi pods
  pending 15 days. Reported to the cluster owner (Preston) via Slack on
  2026-10-30 with full diagnostic evidence; no ticket number at time of
  writing — update this line when one exists.
  Re-checked 2026-11-02: unchanged — `habana.ai/gaudi: 0` in node capacity
  *and* allocatable, device-plugin DS still 0 pods, driver-version label
  still absent. Cluster owner's interim diagnosis ("another user locking
  the GPUs doing coco work") does not match the cluster state: a locked
  card reads capacity 8 / allocatable 0, not capacity 0 — the pending
  coco-workload pods are a symptom (nothing to schedule against), not the
  cause. Intel hardware check initiated Monday 2026-11-02 per Preston;
  the driver-label path is checkable independently of the hardware.

Other environments: models-arch ROSA (CPU-only, GPU operator installed);
flightpath k8s (6 huge CPU nodes, no accelerators); g5.12xlarge AMI kept
(driver, CDI, images, weights baked in — warm restart).

## 8. Model notes

- Qwen3.5 is a **thinking** model: the pipeline sends
  `chat_template_kwargs: {"enable_thinking": false}` per request or `<think'>`
  blocks consume the max_tokens budget and break JSON parsing. Thinking
  *enabled* is the untested quality lever for the judgment tier.
- Serve params for Qwen3.5-35B-A3B (from the model card, in our overlays):
  `--reasoning-parser qwen3`, `--tool-call-parser qwen3_coder`,
  `--trust-remote-code`, `--mm-encoder-tp-mode data`,
  `VLLM_ENGINE_READY_TIMEOUT_S=3600` (72 GB downloads + compile exceed the
  default deadline). The rust frontend is **disabled** — see §7 (NFS
  finding); the Python frontend is the proven path here.
  67GiB bf16; fits 2× Xeon NUMA nodes (matches rhgaendi) or 4× A10G (TP=4).
- Long context: deliberately kept at 16k — workload-sized (CUAD filtered to
  ≤24k chars ≈ 6k tokens); CPU prefill for 85k-token contracts would take
  minutes per request (the plan's V1 concern, measured as the ~50% CUAD arm
  slowdown vs synthetic).
- CUAD specialists: 2021 checkpoints load cleanly in modern transformers;
  DeBERTa-v2-xlarge needs `use_fast=False` (sentencepiece) and scored *below*
  RoBERTa-large on annotation-matched F1 (more aggressive on unannotated
  clauses). Contamination discipline: they were fine-tuned on CUAD's train
  split, so all specialist comparisons run on `test.json` docs only
  (`--split test` in the converter).

## 9. In flight / open

- **Test-split series on OCP — all four variants run, the two specialist
  arms need a rerun (2026-11-02)**: 1.5B F1 0.242 (wall 1100 s); 35B-A3B
  F1 0.712, risk 0.65, 6/20 docs fully correct, 0 violations (wall
  2292 s, ~1760 s device-active). The specialist and specialist-moe arms
  completed mechanically but are **contaminated and not quotable**: both
  lost the local `oc port-forward` tunnel mid-run on a day the SNO's
  networking was unstable (CNI sandbox failures from ~10:30, an API
  blip at 12:11, a wedged kserve controller, pods cluster-wide showing
  8–13 restarts) — every request after the tunnel death failed with
  "All connection attempts failed" (72/121 and 45/121 steps, burst
  pattern recorded in the evidence). The composite pool itself is
  exonerated: it served 100% of the requests that reached it. Rerun
  when the node is stable; consider exposing trace-ui via a Route to
  remove the port-forward from the measurement path. Evidence:
  `runs/cuad-test/`.
- The mixed-split numbers in §3 read optimistic next to the held-out split
  (MoE 0.80 → 0.712): quote test-split numbers only.
- Open items: **the embedding-tier call — all inputs measured** (§11:
  corrected numbers + dinghy-8B; everything ties at the top; decide on
  cost/provenance/simplicity); **specialist + specialist-moe arm rerun**
  (contaminated by the tunnel death, §9 above); end-to-end
  retrieve-then-extract **through the stack** (the 0.800 is a local
  direct measurement); the sandbox-triggered batch / secure-agent wiring
  (openshell CLI to be run in-container per IT policy; gateway installed
  and healthy); thinking-mode A/B for the judgment tier (Qwen3.5
  thinking is the untested quality lever); ContractNLI as the risk-tier
  evaluation; hybrid-bench (concurrency 1/4/16 sweep, TTFT — the V1
  concern); 200-document scale run; V2 tool-calling through the full
  stack; the agent-blocked on-stage moment on OCP.

## 10. Strategic synthesis — what the demo claims, in priority order

1. **Contract review is batch.** Measured: minutes per document on CPU tiers
   is operationally acceptable; the workload has no interactive latency
   requirement. The original "latency justifies GPU" argument is retired and
   is not made on stage.
2. **CPU-only is defensible for batch.** The 35B-A3B MoE served on CPU
   matched or beat the 7B-on-GPU quality (test-split 0.712 and mixed 0.80
   vs 0.761; risk 0.65–0.75 vs 0.6) at minutes-per-doc latency. An all-CPU
   deployment is a legitimate configuration of this stack for a pure batch
   posture.
3. **The hybrid thesis, restated:**
   - *Governance first* — the two-entry matrix with fail-closed sensitive
     routing is the hard result: 0 violations, every stack, every dataset.
   - *Owned-capacity economics second* — **[calculated from measured
     device-time, 200-document batch, cloud list rates]** hybrid ≈ $0.01 of
     rented GPU time (only reasoning steps leave CPU; arm D measured
     0.21 GPU-s/doc on the g5) vs ≈ $0.57 all-GPU (arm A, 9.35 GPU-s/doc);
     rented CPU-only is the worst option (hundreds of core-seconds per
     document at list rates — an order of magnitude above hybrid). On owned
     hardware the variable is utilisation, not rate cards.
   - *GPU placement third* — interactive drill-down (an analyst opening a
     contract), burst absorption, and unproven quality headroom (thinking
     mode) are what GPU is for; batch volume is not.
4. **"Just run a bigger model on GPU" — answered empirically.** That is arm
   A, and it lost on quality to the CPU-served MoE on this workload
   (generation caveat stated, §3.1). If GPU capacity exists, serve *the same
   MoE* there (TP=4 on 4× A10G fits 67GiB bf16): identical quality, seconds
   instead of minutes — the router then chooses on speed/cost alone.
5. **"Same quality, different speed" is the degradation story**, not the
   steady-state pitch: lose latency, never correctness.

## 11. Product direction: contract portfolio intelligence

Agreed evolution: from single-contract review to **portfolio intelligence** —
retrieval-first, evidence-first, governance throughout:

```
PDF/DOCX corpus ─> docling ingestion (CPU) ─> PII screen at ingest
                      │  structure-aware chunks, char offsets preserved
                      v
              litil-embed-0.6b embeddings    (CPU tier, cheap, offline)
                      │  regime split (gate-b result, Monday call pending):
                      │  within-doc retrieve-then-extract = 0.6B;
                      │  cross-doc portfolio queries = 8B index (see below)
                      v
              CUAD specialist span extraction   (ms/clause, cannot hallucinate)
                     │  span JSON + evidence chunks
                     v
             Qwen3.5-35B-A3B synthesis         (risk judgment, cross-doc report)
             every hop through praxis: labels, pinning, audit
```

- **docling** (designed, not built): CPU-native document parsing
  (PDF/DOCX → structured text) as the ingestion front-end; the PII screen
  moves to ingest time so *storage* is governed too, not just inference.
- **Embedding tiers** **[external — MTEB Law v1, via leaderboard CSV,
  2026-10-31]**:

  | model | MTEB(Law) | license | planned role (pre-measurement) |
  |---|---|---|---|
  | litillabs/octen-law-8b-v1 | 76.77 | Apache-2.0 | query side (40,960-token context) |
  | litillabs/litil-embed-0.6b | 73.30 | Apache-2.0 | bulk document side, CPU tier (0.6B) |
  | Hanno-Labs/dinghy-law-8b-v1 | 72.58 | OpenMDW-1.1 | open *training data* (provenance story) |

  Notes: these are embedding models, not generative (dinghy is a
  Nemotron-3-Embed base). None were trained on CUAD (open legal-pair corpora,
  not this dataset), so retrieval comparisons on the test split are
  contamination-clean. The planned tier split (0.6B embeds the portfolio on
  CPU, the 8B embeds queries where latency matters) was the hardware story —
  reinstated in regime form by the gate-(b) result below.
- **Retrieval evaluation — first results [measured, single run]**:
  `hybrid_demo/retrieval/` — `chunking.py` groups paragraphs into
  embedding-window chunks while preserving character offsets (oversized
  paragraphs hard-split on sentence boundaries); `evaluate.py` issues the
  *official CUAD question* per (document, clause-category) pair as a
  retrieval query over that document's chunks (within-document retrieval —
  what a retrieve-then-extract pipeline feeds the extractor) and scores
  recall@k / MRR against the expert-annotated spans. A hit at rank k means
  the retrieved chunk overlaps an annotated answer span. 20 held-out
  test-split documents, 157 pairs; each model's own query/passage prompts,
  cosine similarity; evidence in `runs/retrieval/`.

  | model | MTEB(Law) [external] | R@1 | R@5 | R@10 | MRR |
  |---|---|---|---|---|---|
  | litillabs/litil-embed-0.6b | 73.30 | **0.497** | **0.796** | **0.854** | **0.627** |
  | Hanno-Labs/dinghy-law-0.6b-v1 | 65.83 (8B variant: 72.58) | 0.471 | 0.745 | 0.834 | 0.592 |
  | litillabs/octen-law-8b-v1 | **76.77** | 0.401 | 0.720 | 0.809 | 0.532 |

  Findings:
  - **The leaderboard ordering inverts on this task**: the 0.6B litil beats
    the top-scoring 8B on MTEB(Law) (octen, 76.77) by ~10 points on
    R@1/MRR, and even dinghy-0.6B — 19th on the leaderboard — beats it by
    7 points on R@1: **both 0.6B models out-retrieve the 8B**. On
    within-document clause retrieval over CUAD contracts, the cheapest
    tier measured best — the "0.6B bulk / 8B query-side" tier split above
    is *not* supported by our corpus; the 0.6B serving both sides is the
    leading architecture, pending the clause-specific-prompt experiment
    below.
  - **Decision (2026-10-31, with the user): adopt symmetric
    litil-embed-0.6b** — the same model embeds passages and queries. It
    measured best on the task the pipeline actually performs; the batch
    concession (§10.1) already retired the latency argument for a query-side
    big model; and one embedder serves portfolio indexing, queries and the
    specialist's semantic-cache swap-in (§4.1) — one component fewer to
    deploy and defend on stage. It is also the strongest "method >
    backbone" demo moment: planned from the leaderboard, measured on our
    corpus, shipped the cheap model — with the receipt. **Not locked**:
    two cheap experiments gate it — (a) a clause-specific query-prompt A/B
    (the baseline used each model's generic web-search instruction; litil
    also ships unused legal prompts — LegalQuAD, AILA), and (b) a
    cross-document portfolio eval over the full test split as one index —
    MTEB(Law) is mostly cross-document retrieval, the product's portfolio
    query. Revisit triggers: the 8B winning under a tuned prompt or winning
    the cross-document regime (then asymmetric tiers return, justified by
    measurement). Optional later: dinghy-8b for the OpenMDW
    open-training-data provenance angle — a governance talking point, not
    a performance pick.
  - **Gate results (2026-10-31 evening — both experiments run,
    [measured])**:
    - *Gate (a) passed.* The clause-prompt A/B moves every model by ≤0.03
      and reorders nothing: litil leads under all three query prompts (MRR
      0.627 web-search / 0.635 clause-finding / 0.632 LegalQuAD), the 8B
      stays last (0.532 / 0.549). Paired McNemar on per-pair first-hit
      data (hit@5): litil > octen p = 0.0075, litil > dinghy p = 0.0215 —
      the within-doc inversion is significant, not noise.
    - *Gate (b) fired.* In the cross-document regime — the full test split
      as one index (102 docs, 4,004 chunks, 40 categories; documents
      ranked by best chunk per category) — **the 8B wins decisively**:
      octen MRR 0.940 / success@1 0.900, vs dinghy 0.882 / 0.825 and
      litil 0.829 / 0.725. The MTEB(Law) ordering is restored *in its own
      regime*: the leaderboard is mostly cross-document retrieval, while
      our retrieve-then-extract regime is within-document. The original
      sin was applying a cross-doc leaderboard to a within-doc task.
    - Per the agreed triggers the 8B returns — with one constraint the
      plan missed: litil and octen have incompatible embedding spaces
      (1,024 vs 4,096 dims), so "0.6B passages + 8B queries" cannot share
      one index. The measured-coherent design is **regime-tiered**: the
      0.6B (CPU, batch) owns within-doc retrieve-then-extract; the 8B
      (GPU, interactive) owns the cross-document portfolio-query index,
      embedding both sides of it. Flagged for the Monday call — recorded,
      not re-decided here.
    - Evidence: `runs/retrieval/` — `*-clause-prompt.json`,
      `litil-embed-0.6b--legalquad-prompt.json`, `*--crossdoc.json`, and
      `*--baseline-perpair.json` (verification reruns of the baselines,
      numbers identical — determinism checked; they add the per-pair
      first-hit data the original runs predate). The 8B cross-doc run
      took ~93 min on the laptop GPU (MPS) — a one-off, not a serving
      number.
  - **Correction (2026-11-02): the chunker had an offset bug — all
    retrieval numbers above were re-measured.** The oversized-paragraph
    path in `chunking.py` sliced the *document* with paragraph-local
    sentence offsets, so chunks on ~40% of docs (any doc with a
    >1600-char paragraph) carried text from the document's start and
    wrong ranges — gold spans deep in those documents overlapped **no**
    chunk. Fixed (3 lines) + 5 regression tests (`test_chunking.py`; the
    suite is now 58 tests); every affected number was rerun once and the
    values above are the corrected ones. Headline movements: within-doc
    R@10 0.854 → **0.981** (litil); the ordering conclusions all survived
    (inversion within-doc, gate (a) no-reorder, gate (b) 8B-wins-cross-doc:
    MRR 0.921 vs 0.880/0.866). Significance on corrected per-pair data
    (McNemar, hit@5, exact test; reproduce with
    `cd demo && uv run python tools/mcnemar.py
    ../runs/retrieval/litil-embed-0.6b--baseline-perpair.json
    ../runs/retrieval/octen-law-8b-v1--baseline-perpair.json --k 5`):
    litil > octen **p = 0.0075** (125/157 vs 113/157), litil >
    dinghy-0.6B **p = 0.0215** (125 vs 117), dinghy-0.6B vs octen
    p = 0.4240 (wash, 117 vs 113). Earlier drafts quoted p=0.0001 and
    p=0.0018 from a pre-correction analysis; those values are superseded
    and are not reproducible from the committed per-pair files.
  - **dinghy-8B evaluated (2026-11-02, corrected protocol):** within-doc
    MRR 0.698 (best point estimate; statistically **tied** with litil,
    p=0.134; significantly above octen, p=0.013) and cross-doc MRR 0.912
    (s@1 0.850 — tied with octen's 0.921/0.850). The honest summary:
    **everything ties at the top in both regimes** — the within-doc tie
    is between a 1.1GB and a 15GB model, the cross-doc tie between the
    two 8Bs. The architecture decision is therefore cost and provenance,
    not leaderboard points: litil-0.6B for the batch/CPU tier (tied
    quality at 13× smaller), octen-8B or dinghy-8B for the interactive
    cross-doc tier (quality-tied; dinghy adds the OpenMDW
    open-training-data provenance story). A single dinghy-8B index
    everywhere is viable if simplicity beats tiering, at the cost of a
    15GB model on the batch tier.
  - **End-to-end retrieve-then-extract (2026-11-02, [measured], local
    direct model measurement — not through the stack):** the CUAD
    specialist fed litil-retrieved top-k chunks, scored with the
    pipeline's own scorer on the exact fields of the recorded MoE arm
    (80 extract steps, 19 held-out docs): full-document **F1 0.780**
    (P 0.657, R 0.958), top-5 chunks **F1 0.800** (P 0.667, R 1.000),
    top-10 **0.787**. **Retrieve-then-extract beats the generative MoE
    (0.712) by 9 F1 points**, cannot hallucinate by construction, runs
    at ms/clause on the CPU tier, and the top-5 window even beats the
    specialist's own full-document number (retrieval filters
    wrong-span answers). Error decomposition at top-5 over these 80
    extract steps in 19 documents: **zero retrieval misses and zero
    specialist misses**, so recall is 1.000; the remaining errors are 12
    extra spans (precision 0.667), not misses. That is a statement about this
    scored set, not a general hit rate: on the separate 157-pair
    retrieval probe the same embedder's hit@5 is 125/157 (~80%).
    Evidence:
    `runs/retrieval/e2e-specialist{,-top10}.json`; harness:
    `hybrid-retrieval-e2e`. This is the SOTA-ladder prediction
    (§3.5, method > backbone) confirmed on our stack.
  - Caveats, stated plainly: single run; the query side used each model's
    *generic web-search* instruction prompt, not a clause-specific one (a
    prompt tuned for clause-finding may reorder these); this is a specific
    task, not MTEB(Law)'s aggregate; dinghy was evaluated at 0.6B — its 8B
    leaderboard variant was not.
  - Headline for the pipeline question: top-5 retrieval covers ~80% of
    annotated clauses — retrieve-then-extract with a top-5 window would
    *contain* the answer for 4 of 5 fields. The end-to-end experiment
    (does the specialist on retrieved chunks beat the 0.71–0.80 generative
    baseline, as the SOTA ladder §3.5 predicts) is the next measurement,
    not yet run.
- **Candidate selection, formalized (2026-10-31)**: the MTEB(Law) CSV is
  the *shortlist* filter, the local 157-pair probe is the *decision*.
  Filter: ST-compatible, open weights, text-only (262 → 146), then
  hardware tiers — CPU-bulk ≤1.6GB (98 candidates; litil-0.6B is #1 by
  score, then dinghy-0.6B 65.83, greenleaf-law-tiny 64.49) and the
  GPU/query tier (48 candidates; octen-8B #1, then MoD-Embedding 4B-class
  74.65, dinghy-8B 72.58). The three models we evaluated were already
  the funnel's top picks in both tiers. Regime caveat from our
  measurements: the CSV score tracked our cross-doc regime (gate b) but
  inverted on within-doc (first run) — so shortlist by CSV, then probe
  locally; Tier-A probes cost ~1 min per model. Untested candidates worth
  a look only if constraints change: MoD-Embedding (7.7GB, half the 8Bs'
  memory, fits one A10G), dinghy-8B (OpenMDW provenance story).
- The specialist's semantic cache (§4.1) gets its real embedding from this
  work — litil-embed-0.6b is the swap-in candidate at the stated swap point,
  now backed by the best measured retrieval quality above.

## 12. Upstream alignment (2026-11-03 review)

A review of the upstream projects we build on, and what we should do about
each. Nothing here is upgraded yet; this is the adopted direction.

### llm-d-sc (`llm-d-incubation/llm-d-semantic-classifier`)

We deploy `quay.io/rmalone/llm-d-sc:v0.1.0` with the complexity modelcar —
the current release; nothing newer is stable. The upstream roadmap maps
directly onto our two open architecture items:

| their version | capability | our open item |
|---|---|---|
| 0.23 (open) | multi-signal runtime: built-in signals returned together in one response | **V19** — complexity + sensitivity in one routing chain; today we run two gateway entries with the pipeline's PII screen picking the sensitive one |
| 0.25 (next) | generic domain classification (sequence-classification runtime, ModernBERT-style) | finer labels for contract steps (§4) |
| 0.4 (open) | custom taxonomies: bring your own labels for business routing | **native specialist composition** — extraction→specialist stops living inside the composite pool and moves up into the gateway |
| 0.5 (open) | feedback loop: export classification outcomes for retraining | closes the V14 label-disagreement loop |

Health and readiness probes are now upstream too (PR #42, 2026-10-08): the
server speaks the standard `grpc.health.v1` protocol, which Kubernetes
`grpc:` probes understand. Our pinned `v0.1.0` image predates that change —
the tag was pushed 2026-10-20 but built from the release tag, and the binary
has no health service — so the manifest keeps a TCP readiness probe and a
comment saying why. A `grpc:` probe against this image would leave the pod
never Ready. Adopting the gRPC probe is a reason to move to a newer image,
not a change to make on this one.

Also relevant now: the **sensitivity taxonomy ships built in**
(`classifiers/sensitivity.json`: PUBLIC/INTERNAL/CONFIDENTIAL/REGULATED/
NEVER_EGRESS, anchor-topk-mean method). A cheap, high-value experiment:
run it against our CUAD documents and measure agreement with the
pipeline's PII screen — if it agrees, the screen can move into the
request path (with the fail-closed semantics preserved by the approved-set
design). Their evidence-ledger discipline (machine-checked promotion
gates: `hack/spec-check --promotion` refuses while any gate is unmet) is
the pattern our FINDINGS labelling discipline aspires to.

### Praxis (`praxis-proxy/ai`)

Two repos share the name: `praxis-proxy/praxis` is the Rust proxy framework
(v0.7.5, 2026-10-21), and `praxis-proxy/ai` is the AI gateway built on it.
The gateway is what we deploy, and the versions below are the gateway's.

We run v0.7.0; **v0.9.0 is current** (2026-10-30). Items in v0.8/v0.9
that matter to us:

- **classify in the request-head phase** (v0.8) — plausibly related to our
  cold-gRPC first-request-skips-classification caveat (§7); worth testing
  on upgrade.
- **model/cluster-labeled gateway metrics + TTFT fix** (v0.8) — our
  hybrid-bench TTFT open item gets native support.
- **in-flight per-model tracking filter** (v0.7, unused by us) — an
  alternative to our `/metrics` sampler for device-time measurement.
- **intelligent_route: decisions recorded, gauges tied to what actually
  happened** (v0.7/v0.8) — we route via chains/config today; worth
  evaluating whether intelligent_route formalises our matrix.

Upgrade path: the deployed image is `quay.io/rmalone/praxis-ai-llm-d-sc`,
a fork that carries the `llm_d_sc` filter the upstream gateway does not
have. Its registry publishes only v0.7.0, so v0.7.0 → v0.9.0 means
rebuilding that fork on a newer `ai` release, not changing a tag; treat the
result as a new stack and re-run the config-compatibility checks on the
simulated stack first (0.x configs may change). The framework releases
since v0.7.0 also carry security fixes (v0.7.4 redacts credential-looking
header values from `--dump` and the admin pipelines view; v0.7.5 updates
rustls-webpki for RUSTSEC-2026-0382 and rustls for ECH/Brotli issues) —
they matter if TLS upstreams or the admin surface are ever enabled. The
org also hosts an **operator** (Kubernetes CRD-based deployment — a future
alternative to our plain Deployments + config-watcher arm swaps), a
**policy engine (PPE)** (a candidate home for the
sensitivity-is-a-constraint matrix), and a **demos archive** (a natural
contribution target for this demo once restructured).

## 13. Measurement-compatibility notes (2026-11-03 corrections)

A review pass (external) fixed several real defects in the measurement
code. Each fix is correct; each also changes what the numbers mean, so
the recorded values carry these flags:

- **The risk rubric is now one shared function** (`pipeline/risk.py`),
  asked in the prompt and applied to the truth — and it adapts to whether
  the dataset annotates a data-protection category. Recorded risk
  accuracies (MoE 0.65, §3.3) were measured under the old always-high
  rubric on CUAD; **risk numbers from new runs are not comparable** to
  them. This is the §3.3 pitfall fixed, not a regression.
- **CUAD report accuracy was structurally deflated**: the scorer demanded
  a `missing_data_protection` count the prompt never asked for (CUAD has
  no DP category). Report-accuracy numbers from new runs will read
  higher for the right reason.
- **The pipeline's concurrency was cosmetic**: documents were awaited one
  at a time, so `--concurrency` never parallelised and **every recorded
  wall-clock time was a serial time**. Device-time per document — the
  metric FINDINGS actually quotes — is unaffected. New runs' wall times
  will be genuinely parallel and much shorter.
- **The specialist's data-protection alias was a wrong-answer bug**: DP
  prompts were silently answered with the audit-rights question. DP
  extraction now forwards to the general model (correct behaviour).
  Specialist-arm DP fields from old runs are unreliable.
- **Sensitivity truth on CUAD is honestly absent** (`sensitive: None`):
  the converter no longer uses the pipeline's own PII screen as a truth
  label (circular). Screen precision/recall is now computed only on
  independently labelled sets (synthetic); CUAD screen output remains an
  observation ("5–12 of 20 docs flagged"), not an accuracy. Governance
  counting is unchanged — it was always enforcement-of-the-flag, using
  the screen's own decision.
- **Chunking hard-splits pathological sentences** (no punctuation for
  thousands of characters) that could still exceed the embedding window;
  offsets translated correctly, regression-tested. Micro-shifts in
  retrieval numbers on affected documents are possible.
- **The trace relay no longer persists sensitive document text by
  default** (routing metadata only) and supports an optional bearer token
  for the trace API — required hygiene now that trace-ui is exposed via
  Routes.
