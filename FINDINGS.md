# Findings — Private, governed agents on hybrid CPU+GPU inference

Working record of everything measured and learned, newest sections last.
CUAD = Contract Understanding Atticus Dataset (510 expert-annotated
commercial contracts; CC BY 4.0, confirmed on the Atticus Project site,
accessed 2026-10-30). Every
number here is reproducible from the runbooks in the [README](README.md); the
reports referenced live under `runs*/` per stack. Claims are labelled
**[measured]** (our numbers, our replay), **[observed]** (single-run or
qualitative), or **[external]** (cited, not ours).

**Naming:** the configs now have behavior names: **one-pool** (old
`all-gpu`, key A), **small-first** (old `gpu-small`, key B),
**round-robin** (key C), **hybrid** (old `matrix`, key D). The old names and
letters remain aliases. Historical run directories and the recorded
`summary.mode` strings keep their original labels; the dashboard and trace
UI present the canonical name for old runs.

**Run archive:** the working tree keeps the current quoted run, the same-day
control, and the CUAD/retrieval evidence. Older run directories named below
were pruned from the checkout; recover any of them from git history at
`ea80dfb` with `git checkout ea80dfb -- <path>`. See `runs/README.md`.

**Metric definitions:** precision, recall, F1 and the containment matcher are
defined in [design.md § Precision, recall and F1](docs/design.md#precision-recall-and-f1).

## 1. What was built

Three runnable instances of the same demo, byte-identical praxis routing
configs across all of them (`manifests/base/praxis/`):

| stack | where | pools | purpose |
|---|---|---|---|
| Linux/podman, simulated | any host (RHEL 10 CPU box used) | fake OpenAI-compatible pools with deterministic tiered error rates | CI, choreography, fast iteration |
| Linux/podman, real | g5.12xlarge (4× A10G, stopped, AMI kept) | real vLLM: 1.5B CPU, 7B/1.5B GPU | the measured GPU-vs-CPU story |
| OpenShift | rhgaudi3s2 SNO (4.22, 288 cores, 2.3TB, free) | real vLLM, all pools CPU-served (`accelerator: cpu` label) | full-platform validation: routing, governance, sandbox, measurement on OCP |

Plus the **CUAD specialist**: the paper's fine-tuned RoBERTa-large QA
checkpoint (354M, Zenodo) behind an OpenAI-compatible FastAPI wrapper. In
the first build it was a *composite* CPU pool — extraction prompts answered by
the QA model in one encoder pass, everything else forwarded to the general
model behind the same relay. The 2026-11-10 rebuild split it into its own
`cuad-specialist` route, so there is no in-pod forwarding (FINDINGS §15.10);
the composite rows in §15 describe the older graph and stay labelled as such.

Architectural invariants: every through-stack request goes batch runner →
trace-ui → **praxis** → pool. The route is selected by the policy's
`X-Demo-Step` table; the original build used llm-d-sc complexity labels, and
the classifier was measured, found to label 1,200/1,201 steps MEDIUM, and
removed from the request path on 2026-11-05 (§15.5). The trace UI's SQLite is
the audit trail; governance is verified from the gateway's own records, not
the batch runner's.

## 2. Governance and routing — the headline, replicated on every stack **[measured]**

Sensitive-document steps served outside the approved pool set
(`vllm-cpu`, `vllm-gpu-approved`), by arm:

| config | violations | CPU-served share |
|---|---|---|
| A one-pool (old all-gpu) | 25/25 | 0% |
| B small-first (old gpu-small/gpu-split) | 25/25 | 0% |
| C round-robin | 12–24 (varies by run) | ~50% |
| **D hybrid (old matrix)** | **0 — on every stack, every dataset** | ~99% |

The table is the replicated containment result from the earlier stacks. On
the current 2026-11-12 RC1 ROSA record the hybrid share is **83.3%
CPU-served** (the same route split), with **247/247** sensitive steps
approved, **0** violations and **0** errors (§15.12). The three ungoverned
controls in that RC1 matrix are no longer quality comparisons: they send
every document through the general entry, where the PII gate denied 246
sensitive steps and one content miss per run reached a non-approved route.
The 2026-11-11 `v0.9.1` matrix remains the last comparable four-arm routing
measurement (one-pool and small-first 247 violations, round-robin 110;
§15.11). The difference is the entry boundary, not a change in the
containment claim.

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
  arms need a rerun (2026-11-02)**: the synthetic through-stack gap is now
  closed (2026-11-04 ROSA run: 600/800 extraction calls answered by
  `cuad-specialist`, §15.4); what remains open is the rerun on the CUAD
  test split. 1.5B F1 0.242 (wall 1100 s); 35B-A3B
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
- **CPU model choices vs the vLLM xeon6 catalog — a ranked test plan, not a
  change (2026-11-04 scan)**: recipes.vllm.ai (221 recipes, 40 `hw=xeon6`
  renderings) shows the deployed `Qwen3.5-35B-A3B` is the exact xeon6
  recipe — BF16 only, **no CPU-quantized variant** (the GPTQ/FP8 checkpoints
  render for XPU, not xeon6) — while `Qwen3.5-9B` and `Qwen2.5-1.5B` have
  no xeon6 recipe at all. No manifest changed; this repo's rule is measured,
  not catalog-based. Plan: keep `Qwen3.5-35B-A3B` (the cpu-real-moe flags
  already match the rendered recipe; `VLLM_ENGINE_READY_TIMEOUT_S ≥ 1800`
  stands); benchmark `openai/gpt-oss-20b` (3.6B active, native MXFP4,
  ~16 GB — fits a 16-core/64 GiB pool with real KV headroom) as the
  CPU-synthesis challenger through the CUAD scorer; `Qwen3-30B-A3B` is the
  conservative MoE fallback (no experimental Mamba path); if the dense
  fallback is ever used for a quoted comparison, switch to `Qwen3-14B` or
  say plainly the 9B is off-catalog. The CUAD specialist and the embedders
  keep their custom serving — vLLM has no recipes for extractive QA or
  those embedders. Warnings for any test: the xeon6 profile is a
  **multi-socket** node, so a recipe is a validated flag set, not a capacity
  claim for our 16-core NUMA-local pool slices — never cite the 40 recipes
  as "supported on our CPU pool"; changing `--served-model-name` breaks the
  relays (all CPU pools serve as `Semantic-Qwen`); cold start, not steady
  state, is the rollout risk (seed caches, long readiness timeouts).
  Ranked test order: 35B-A3B (incumbent) → gpt-oss-20b → Qwen3-30B-A3B.
  The full review with rendered commands is preserved in git history
  (`docs/handoff-2026-11-04-cpu-model-recipes.md` at commit `94a9572`).
- Open items: **the embedding-tier call — all inputs measured** (§11:
  corrected numbers + dinghy-8B; everything ties at the top; decide on
  cost/provenance/simplicity); **specialist + specialist-moe CUAD
  test-split rerun** (the synthetic through-stack run is done, §15.4; the
  earlier split arms were contaminated by the tunnel death); end-to-end
  retrieve-then-extract **through the stack** (the 0.800 is a local
  direct measurement); the sandbox-triggered batch / secure-agent wiring
  (openshell CLI to be run in-container per IT policy; gateway installed
  and healthy); thinking-mode A/B for the judgment tier (Qwen3.5
  thinking is the untested quality lever); ContractNLI as the risk-tier
  evaluation; hybrid-bench (concurrency 1/4/16 sweep, TTFT — the V1
  concern); V2 tool-calling through the full stack; the agent-blocked
  on-stage moment on OCP.

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

Upstream publishes no image for this service, so "newer" means building it.
On the SNO (2026-11-03) the service now runs a build from upstream main
(`e660fff`): the binary serves `grpc.health.v1`, and an in-cluster
`llm-d-sc-classify --model /models` returned COMPLEX 0.999 on a design
prompt, so the modelcar and the wire contract still line up. The base
manifest keeps the public `v0.1.0` image and its TCP probe so other clusters
can deploy without building; `oc apply -k manifests` restores that image, and
the runbook has the build-and-pin commands.

Also relevant now: the **sensitivity taxonomy ships built into the
llm-d-sc checkout** (upstream `classifiers/sensitivity.json`, not this
repo: PUBLIC/INTERNAL/CONFIDENTIAL/REGULATED/NEVER_EGRESS,
anchor-topk-mean method). A cheap, high-value experiment:
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
rebuilding that fork on a newer `ai` release, not changing a tag. A static
check against the v0.9.0 checkout (which pins praxis 0.7.5) found every key
the four configs use still present — `admin.metrics_address` (binds
publicly without `allow_public_admin`),
`insecure_options.allow_private_upstreams`,
`runtime.subrequest_circuit_breaker`, the
per-cluster `health_check` fields, and the core filters `request_id`,
`access_log`, `static_response`, `iterative_request_router` — so the YAML is
schema-compatible and porting or rebuilding the `llm_d_sc` filter is the
actual upgrade work. Even so, treat the rebuilt stack as new and re-run the
measurement arms: routing behaviour can change under an unchanged config.
The framework releases
since v0.7.0 also carry security fixes (v0.7.4 redacts credential-looking
header values from `--dump` and the admin pipelines view; v0.7.5 updates
rustls-webpki for RUSTSEC-2026-0382 and rustls for ECH/Brotli issues) —
they matter if TLS upstreams or the admin surface are ever enabled. The
org also hosts an **operator** (Kubernetes CRD-based deployment — a future
alternative to our plain Deployments + config-watcher hot-swaps), a
**policy engine (PPE)** (a candidate home for the
sensitivity-is-a-constraint matrix), and a **demos archive** (a natural
contribution target for this demo once restructured).

Executed 2026-11-05: the rebuild and the classifier are published images.
Gateway
`quay.io/mtahhan/gci-praxis:v0.9.0`
(`sha256:2187ab818efab9a8f3d1f911190d47fad960888bada7d3db8a973d3ad70ac992`),
classifier `quay.io/mtahhan/gci-llm-d-sc:main-e660fff`
(`sha256:d94c1f7226bfbaeb5a300f0efdbab0e8a8988fc16af7fd80cff0e008a11fa3e1`),
modelcar mirrored unchanged
(`sha256:299874af83c565d8435c8e061b87da1754cdd918ca15e977a7fecbf3aa17f491`,
byte-identical to the source). All three come from the `publish-images`
workflow (GitHub-hosted amd64 runner; the dev machines are arm64). The
llm-d-sc readiness probe switched from TCP to `grpc:`: upstream `e660fff`
binds its gRPC listener only after the resident classifier loads and warms
and `grpc.health.v1` reports SERVING from that point, so the probe gates on
model warmth, not just a bound socket. The digests above are the
reproducibility boundary for measured runs on the new pair. The opt-in
`deploy/openshift/praxis-0.9.0/` overlay is retired (base now carries
v0.9.0); live clusters run the older pair until rolled, and the §12 warning
stands: treat the rebuilt stack as new and re-run the measurement arms.
Of the three images, only the gateway is consumed by the base manifests:
the classifier was removed from the default stack the same day (§15.5
follow-on), and its image and the modelcar mirror stay published for the
contract-step taxonomy experiment. The design rationale and the digests
table are in `docs/design.md` (§3.2, §6).

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
- **Two further report checks could never pass** (found 2026-11-03 while
  building the stage UI). `governing_law_counts` compared the model's law
  names ("New York") against truth keys holding the whole annotated
  clause ("the laws of the State of New York"); and `highest_risk_ref`
  accepted only the alphabetically first high-risk document id, although
  13 of 19 CUAD test documents are truth-high. The scorer now normalises
  both sides to a jurisdiction name (`jurisdiction()`) and accepts any
  truth-high id. CUAD report scores under the fixed scorer: **0.645**
  (GPU MoE v2), **0.658** (GPU MoE, CPU MoE v2), **0.684** (CPU 9B v2).
  Report numbers recorded before this fix measured an unwinnable check and
  are not comparable. Separately, the thinking arm's report record parses
  to `{}` (the response is truncated or wrapped), so its report score of
  0.0 is a parser failure, not a quality measurement.
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
- **The direct arm asked the wrong clause** (found 2026-11-03): it used
  `generate.CLAUSE_KEYS`, which still included `data_protection`, while
  the CUAD manifest names `audit_rights`. Every run therefore asked for a
  category CUAD does not annotate (19 truth-less fields that scored free
  when the model said "not found") and **never asked for audit rights at
  all** (19 annotated fields scored wrong because they were unanswered).
  The arm now reads `clause_keys` from the manifest and falls back to the
  default only for manifests that predate the field. Corrected CUAD
  test-split numbers on the GPU 35B-A3B arm: F1 0.690 (P 0.625 / R
  0.769), risk 0.632, report 0.645, 5/19 documents fully correct. **CUAD
  quality numbers recorded before this fix are not comparable** — the
  earlier 0.712 came from a different field set, not a better model.
- **Two CUAD titles can collide on the 48-character doc_id prefix**: the
  second contract's text file overwrote the first, so the manifest
  carried 20 entries while only 19 documents existed on disk and the
  scored set silently shrank. `pipeline/cuad.convert` now suffixes
  collisions (`-2`, `-3`, …) and a test covers it. The existing
  `data/cuad-test` set was deliberately **not** regenerated: keeping the
  document set fixed preserves comparability with every run already
  recorded. That set's effective size is 19 documents.

## 14. Benchmark methodology and the GPU sweep (2026-11-03)

Two instruments, one prompt source. No load generator scores answers, so
CUAD quality stays with `hybrid-direct-arm` (manifest clause keys, our
own scorer, §13). Performance comes from `vllm bench serve`, bundled in
the serving image, fed by `tools/bench_prompts.py` with the prompts the
pipeline actually sends.

Options considered and rejected: GuideLLM measures the same single-turn
HTTP surface, adds an install inside the serving container, and its
arrival-rate sweep is already covered by `vllm bench serve --request-rate`
/ `--burstiness`; llm-d-benchmark orchestrates a cluster and would measure
its own scheduler; ShareGPT and `random` prompts do not reproduce
contract-prefill lengths. The custom JSONL flattens system+user into one
message, so token counts differ from a two-message request by template
tokens only (measure before quoting absolute prompt lengths).

**Multi-turn is not part of this workload**, and no `vllm bench serve`
dataset mode supports it anyway (vLLM 0.33.0 `benchmarks/datasets/`):
`custom` wraps one `{"role": "user"}` message and ShareGPT replays
`conversations[0]` as prompt and `[1]` as completion — no assistant turn
is ever fed back. The pipeline sends 115 independent requests per run
(19 classify + 76 extract + 19 risk + 1 report), each carrying its own
document text. A multi-turn transcript loop would be a different
workload and would need a client, not a dataset flag.

Workload shapes (CUAD test split, 19 unique documents):

- **extract-only** — 76 prompts, ~3.0k input tokens each (229,997 total),
  200 output tokens per request.
- **full mix** — 115 prompts, 302,252 input tokens, 23,900 output tokens:
  per-step budgets enforced with `--custom-output-len -1`, and the
  report prompt replayed from the scored run so it keeps its real
  ~8.4k-token length instead of a synthetic one.

Server: Qwen3.5-35B-A3B, 4× A10G, TP=4, `--max-model-len 16384`,
`--max-num-seqs 32`, `--gpu-memory-utilization 0.92`, thinking disabled.
`--num-warmups 2`, `--max-concurrency` 1/4/16, single sweep at a time.

| workload | conc | req/s | TTFT p50/p95 ms | TPOT p50 ms | E2EL p50/p95 ms |
|---|---|---|---|---|---|
| extract | 1 | 0.46 | 686 / 1038 | 7.6 | 2206 / 2565 |
| extract | 4 | 0.80 | 1266 / 2376 | 18.4 | 4976 / 6331 |
| extract | 16 | 1.13 | 1977 / 7436 | 60.9 | 14364 / 19185 |
| mix | 1 | 0.46 | 644 / 1040 | 7.6 | 2119 / 2566 |
| mix | 4 | 0.83 | 808 / 1874 | 18.5 | 4337 / 6133 |
| mix | 16 | 1.05 | 1143 / 6049 | 62.2 | 13635 / 16498 |

Readings:

- **The knee is at concurrency 4.** c=1→4 gains 1.75× throughput at both
  shapes; c=4→16 gains only 1.41× (extract) and 1.27× (mix) while p50
  E2EL grows ~2.8×. Peak concurrent requests reached 19–23 against a
  32-sequence cap: past c≈4 the queue, not the client, sets latency.
- **A full 115-request pipeline run takes 138 s at c=4 and 109 s at
  c=16.** That is the whole per-document portfolio pass, including the
  cross-document report; the batch posture in §10.1 holds comfortably.
- **The report request is not the p95 driver.** Mix TTFT p95 at c=16
  (6.0 s) is *lower* than extract-only (7.4 s) although the report
  prompt is ~3× longer than an extract prompt, because the report is
  last in the file and starts when slots have freed.
- TPOT p50 rises 7.6 → 18.5 → 62 ms and ITL p95 reaches ~330 ms at
  c=16 — decode is bandwidth-bound at this batch size on four A10Gs.
  Any service-level objective should quote c=4, not c=16.

Raw result JSONs: `runs/cuad-test/perf/`.

**Cross-hardware parity (same weights, CPU vs GPU).** The same
Qwen3.5-35B-A3B was run through the direct arm on CPU (port 8002) and on
4× A10G. At the metric level the two runs agree: F1 0.690 (P 0.625 /
R 0.769), risk 0.632, 5/19 documents fully correct on both; report 0.658
on CPU vs 0.645 on GPU, the difference coming only from governing-law
counts. At the record level they are not identical: 93 of 115 record
contents match exactly, and the 22 that differ are token-level
divergences under `temperature: 0` (different section reference,
different but valid termination excerpt, reworded risk reasons) — CPU
and GPU kernels are not bit-identical. Per-request latency is the real
difference: CPU classify took 3.4–21.3 s against ~0.6–1 s on four A10Gs,
so CPU is for isolation and governance posture, not interactive latency.

## 15. ROSA re-baseline: 200-document matrix, sensitivity, cost (2026-11-04)

Environment: ROSA HCP `<redacted-cluster>` (us-east-1a; cluster
`<redacted-ocm-cluster-id>`), namespace `openshell-demo`. CPU node
`x8i.24xlarge` (48 cores, 1.5 TiB) and GPU node `g5.12xlarge` (4x A10G,
TP=4) in the same AZ; pools `<redacted-gpu-pool>` and `<redacted-cpu-pool>`. Raw
records and reports: `runs/rosa-200/` (archived at `ea80dfb`).

This section replaces the SNO numbers for every demo-facing claim. The SNO
runs remain the validation record; they are not the demo environment.

### 15.1 Stacked matrix (arms A-D, 200 synthetic documents, 41 sensitive, c=12)

| arm | wall | extract F1 | risk acc | report acc | fully correct | CPU-served | sensitive approved | violations | GPU-s/doc |
|---|---|---|---|---|---|---|---|---|---|
| A all-GPU | 202.2 s | 1.000 | 1.000 | 0.756 | 200/200 | 0.0% | 0/247 | 247 | 1.974 |
| B GPU-small 1.5B | 438.9 s | 1.000 | 0.395 | 0.596 | 77/200 | 0.0% | 0/247 | 247 | 2.218 |
| C round-robin | 612.9 s | 0.992 | 1.000 | 0.750 | 188/200 | 50.0% | 125/247 | 122 | 2.855 |
| **D matrix** | **1111.8 s** | **0.982** | **0.995** | **0.764** | **173/200** | **99.9%** | **247/247** | **0** | **0.036** |

Zero errors in all four arms. The screen was perfect on this dataset in every
arm: 41 true positives, 0 false positives, 159 true negatives, 0 false
negatives.

What the matrix says:

- **Governance is the result, not the routing elegance.** C (round-robin, no
  classifier) leaks 122 of 247 sensitive steps; A and B leak all 247 by
  construction; D leaks none. That separation is the demo.
- **The 35B-A3B on CPU carries judgment.** D loses 1.8 F1 points and 0.5
  risk points against all-GPU, and its report accuracy is slightly higher
  (0.764 vs 0.756). The 1.5B on GPU collapses on risk (0.395, below the
  0.485 majority-class baseline, 77/200 fully correct): "small" is not the
  same as "CPU".
- **GPU time is the decisive number.** D spends 0.036 GPU-seconds per
  document against 1.974 for A: 7.3 vs 394.8 GPU-seconds for the batch,
  54x less. The GPU is used for one step (the cross-document report) and
  nothing else. This is a **CPU-first policy with the GPU reserved for
  cross-document synthesis**, not complexity routing: the classifier sent
  every single-document step to MEDIUM/CPU (§15.5).

### 15.2 Sensitive vs non-sensitive split (arm D, labels from the dataset)

| class | docs | steps | extract F1 | risk acc | fully correct | routes |
|---|---|---|---|---|---|---|
| non-sensitive | 159 | 954 | 0.979 | 0.994 | 135/159 | `vllm-cpu`:954 |
| sensitive | 41 | 246 | 0.990 | 1.000 | 38/41 | `vllm-cpu`:246 |

The split uses the dataset's `sensitive` label, never the pipeline screen
(that would be circular). The governance counter totals 247 sensitive
steps, one more than the 246 per-class steps: it includes one
document-level step beyond the per-document sequence. That extra step is
also the one step the classifier labelled REASONING and routed to the
approved GPU pool. Quality is not bought by leaking: the sensitive class
scores at least as well as the non-sensitive class on both extraction and
risk.

### 15.3 Cost accounting, and what is deliberately n/a

The report separates **device time that was measured** from **device time
that was not**. GPU time is measured per pool and is the number the
economics rest on: 0.036 GPU-s/doc for D, 1.974 for A. At an illustrative
$1.10/GPU-hour that is about $0.002 of GPU time for the D batch against
about $0.12 for A (GPU time only).

CPU core-seconds per document are **not reported**: the relay-fronted CPU
pool did not expose parseable device gauges, and `metrics.json` kept only
summaries. The report prints `n/a` — never `$0.0` — for an unmeasured
pool and withholds the dollar sweep rather than inventing a price. A fresh
20-document arm-D probe with a working sampler is the way to close this;
it does not change the GPU-time argument.

### 15.4 The specialist was actually in the path

Arm D's extraction is not the 35B doing clause extraction directly. Every
extraction record names its responder, and **600 of the 800 extraction
calls were answered by `cuad-specialist`** (governing law, liability cap,
termination — 200 each); the remaining 200 (data protection) stayed on the
CPU MoE (`Semantic-Qwen`). All 800 were served and all routed
`x-demo-route=vllm-cpu`, the composite. Derivation and counts:
`runs/rosa-200/specialist-metrics-2026-11-04.txt`. That is the
through-stack evidence the earlier "0.800 specialist" local run lacked.

### 15.5 The classifier is prompt-difficulty, not per-step complexity

Against the generator's per-step labels the classifier agrees 0.1% of the
time (1/1201): `SIMPLE->MEDIUM` 1000, `COMPLEX->MEDIUM` 200,
`REASONING->REASONING` 1. That number is a **taxonomy mismatch**, not a
degraded model. Replaying the exact arm-D prompts through the image's own
`llm-d-sc-classify` (offline, in the pod; `runs/rosa-200/classifier-replay-2026-11-04.txt`):

| step prompt | MEDIUM | SIMPLE | COMPLEX | REASONING | routed |
|---|---|---|---|---|---|
| classify contract (808 ch) | 0.992 | 0.024 | -0.101 | -0.384 | CPU |
| extract termination (842 ch) | 0.993 | 0.002 | -0.092 | -0.377 | CPU |
| assess risk (844 ch) | 0.991 | -0.000 | -0.112 | -0.351 | CPU |
| portfolio report (37,302 ch) | 0.398 | -0.496 | -0.392 | **0.707** | approved GPU |

The working boundary is **single-document analysis -> CPU** vs
**cross-document synthesis -> GPU**. SIMPLE and COMPLEX are never emitted
for this workload, and no exposed setting changes that: llm-d-sc exposes
only the taxonomy name (`complexity`/`cost`/`sensitivity`), model
directory, workers and cache; praxis's `min_score` cannot separate
margins of 0.3-0.99. The demo claim is therefore containment plus
GPU-first economics, not "the AI decides what is hard".

Follow-on (2026-11-05): **llm-d-sc was removed from the default stack.**
The judgment entries (:8086/:8087; replaced by `X-Demo-Step` routes in the
2026-11-10 rebuild, §15.10) already route risk/report stage-aware
without a classifier, and everything else the classifier saw was labelled
MEDIUM -> CPU anyway, so the general (:8080) and sensitive (:8083)
listeners are now **CPU-first for batch work** (general fails over to the
general GPU with the visible degraded flag; sensitive keeps the
approved-only failover set, preserving fail-closed). The 2026-11-10 rebuild
keeps the two-entry containment boundary and moves per-step pool choice to
the router table (§15.10). The rebuilt graph is measured in §15.10; the
numbers in §15.5-§15.9 came from the earlier classifier-in-path config.
The classifier-routed arm B
config and the llm-d-sc Deployment are preserved in git history
(`manifests/base/praxis/arm-b.yaml`, `manifests/base/llm-d-sc.yaml` at
commit `98dc861`); the contract-step taxonomy remains the measured path
back to semantic routing (`docs/design.md` §5).

### 15.6 Gaps this round leaves open

- CPU core-seconds per document (above). GPU time and wall time are
  measured; core-time is not.
- The OpenShift sandbox blocked-action moment is still unverified: no
  sandbox workload or manifests live in this repository (OpenShell /
  OpenClaw are external).
- The 200-document direct arm exposed a report-input bug: the report
  prompt used one line per extraction record (about 229k chars, 57k
  tokens) against a 16k max-model-len, so it failed with HTTP 400 after
  all per-document work had run. The fix is one compact line per document
  (37.5k chars, the size the stacked run already proved), with a
  regression test.
- **Warnings that survive any routing rerun** (from the 2026-11-05 routing
  review; the analysis with the option table is preserved in git history,
  `docs/handoff-2026-11-04-rosa-routing.md` at commit `94a9572`): do not
  just repoint the specialist composite's `--forward-to` at another pool —
  it forwards outside praxis and would bypass the approved set; the pool
  relay's `X-Demo-Route` header hides the composite's real backend, so read
  the composite's own logs for where forwarded calls actually went; and the
  27 failed documents are a specialist **termination-taxonomy** issue (the
  synthetic clause is termination for cause; the CUAD question asks
  termination for convenience), not a routing issue — do not count them
  against any routing change.

### 15.7 Direct baselines (no router, same 200 documents)

Each direct arm sends all 1,201 requests to one bare pool; there is no
classifier, no screen and no approved-pool check. Raw records:
`runs/direct/{cpu-moe,gpu-moe,cpu-small}` (archived at `ea80dfb`); combined
report: `runs/rosa-direct-vs-stacked/report.md` (archived at `ea80dfb`).

| arm | pool | wall | extract F1 | risk acc | report acc | fully correct |
|---|---|---|---|---|---|---|
| cpu-moe | 35B-A3B, 16 CPU cores | 2095.2 s | 1.000 | 1.000 | 0.746 | 200/200 |
| gpu-moe | 35B-A3B, 4x A10G TP=4 | 228.3 s | 1.000 | 1.000 | 0.768 | 200/200 |
| cpu-small | 1.5B, 12 CPU cores | 594.3 s | 1.000 | 0.395 | 0.212 | 77/200 |
| D stacked | mixed, governed | 1111.8 s | 0.982 | 0.995 | 0.764 | 173/200 |

- **Routing overhead is negligible.** Direct GPU and stacked arm A run the
  same workload in 228 s vs 202 s; the governed path does not add wall time.
- **All-CPU is 9.2x slower than all-GPU** (2095 vs 228 s) and 1.9x slower than
  D (1112 s). The specialist explains the gap: stacked D moves 600 of 800
  extraction calls off the MoE, while direct CPU keeps all of them there.
- **The 1.8 F1-point cost in D is the specialist, not the device.** Both
  direct arms score extraction F1 1.000; D scores 0.982 with
  `cuad-specialist` answering 600 extractions.
- **Report accuracy tracks the GPU**: D 0.764, direct GPU 0.768, direct CPU
  0.746 - consistent with the report step being GPU-served in D.
- **Small is the failure mode, not CPU.** Direct `cpu-small` (1.5B) collapses
  on risk (0.395) and report (0.212), matching stacked arm B, which runs the
  same weights on GPU. Judgment quality follows the model.
- **Direct arms are ungoverned by construction**: all 247 sensitive steps hit
  the single pool. The report prints `n/a`, not 0, for their routing cells;
  D is the only arm that is both fast (1112 s) and compliant (0 violations,
  247/247 approved, 0.036 GPU-s/doc).
- Direct device time was not sampled, so their GPU/CPU seconds and dollar
  figures are `n/a`; wall-clock is the comparison. The direct arm ran at
  concurrency 8 vs 12 for the stacked matrix, so the ratios are indicative,
  not a controlled throughput benchmark.

### 15.8 Classifier-free re-measurement, GPU termination route (2026-11-09)

`runs/rosa-200-2026-11-09-gpu-term` (archived at `ea80dfb`) is a **measured
alternative route, not the current policy** (§15.9 supersedes it). The hybrid arm was rerun with
termination extraction on the GPU-bound entry; the one-pool, small-first and
round-robin arms were copied from the same-day run because the routing change
only applies to the governed hybrid. §15.1-§15.7 are the earlier
classifier-in-path record and must not be mixed with it.

The table uses the current config names. These runs were recorded under the
retired labels `all-gpu` and `gpu-small`; the run directories keep those
labels.

| config | wall | extract F1 | risk acc | report acc | fully correct | CPU-served | sensitive approved | violations | GPU-s/doc |
|---|---|---|---|---|---|---|---|---|---|
| one-pool (recorded as all-gpu) | 202.83 s | 1.000 | 0.995 | 0.756 | 199/200 | 0.0% | 0/247 | 247 | 1.013 |
| small-first (recorded as gpu-small) | 454.21 s | 1.000 | 0.395 | 0.278 | 75/200 | 0.0% | 0/247 | 247 | 2.241* |
| round-robin | 570.81 s | 0.991 | 1.000 | 0.756 | 186/200 | 50.0% | 129/247 | 118 | 1.773 |
| **hybrid** | **726.77 s** | **1.000** | **1.000** | **0.756** | **200/200** | **66.6%** | **247/247** | **0** | **0.508** |

Zero errors and zero fallbacks in all four arms. The screen was 41/0/159/0.
Hybrid judgment steps were 201/201 on GPU.

- **The governance result is unchanged.** Hybrid is the only config with 0
  violations and 247/247 sensitive steps on approved pools.
- **The economics changed because the policy changed.** Hybrid sends
  termination extraction (200 steps) plus risk/report (201) to GPU-bound
  entries. It uses ~2.0x less GPU time than one-pool, recorded as `all-gpu`
  (0.508 vs 1.013 GPU-s/doc; 101.6 vs 202.7 GPU-s total), and wall time is
  726.77 s — faster than the 802.85 s run where termination was on the CPU
  specialist.
- **GPU-s/doc counts a physical backend once.** `gpumoe-vllm-approved` selects
  the same pod as `gpumoe-vllm`; the scorer maxes a declared shared group
  instead of summing both routes. Without that correction the figures are
  doubled (1.991 / 0.398) and the saving reads ~6x. Raw samples are unchanged.
- **`small-first` (recorded as `gpu-small`) is CPU hardware (`*`).**
  `vllm-gpu-small` is route-accounted as GPU but the pool map records the x8i
  CPU node; 2.241 is not GPU time. It is a negative control: risk 0.395 and
  report 0.278 show the small model collapsing, not a device effect.
- **Termination recall was a taxonomy issue, fixed by routing.** The previous
  hybrid run scored 173/200 because all 27 misses were the same synthetic
  template — “terminated only for material breach…” — which is **termination
  for cause**. The specialist is asked CUAD's canonical **“Termination For
  Convenience”** question (`specialist/questions.py`): “Can a party terminate
  without cause?” Under that narrower question, `found=false` is defensible.
  It still returned 21 of the 48 cause clauses, and all 152 convenience
  clauses; this was a **question/taxonomy mismatch**, not a clean capability
  gap. The general 35B model extracts both forms, so `extract:termination` now
  uses the GPU-bound entry (`vllm-gpu`; approved pool for sensitive
  documents). The other three extraction clauses stay on the CPU specialist.
  The new run scores extraction F1 **1.000**, **200/200 fully correct**, and
  precision 1.000. Another CUAD expert inherits the same question; the route or
  the label is the fix, not a different specialist.
- `runs/rosa-200-2026-11-09-gpu-term/report.md` (archived at `ea80dfb`) carries the corrected cost
  notes; the run recorded `git_dirty: true` because the routing change was in
  the working tree at run time.

The three direct baselines in §15.7 have not been rerun on the 2026-11-09
stack; treat them as the earlier comparison until that run is repeated.

### 15.9 CPU termination with the conditional cause follow-up (2026-11-09)

> Directory note: the 2026-11-09 matrix and rerun were pruned from the working
> tree after the 2026-11-11 promotion. Recover them with
> `git checkout d551c7b -- runs/rosa-200-2026-11-09`. The measurements below
> remain valid for the graph that produced them.

`runs/rosa-200-2026-11-09-cpu-term-rerun` is the pre-rebuild classifier-free
record; its conditional-cause termination policy is carried into the rebuilt
graph measured in §15.10. Termination extraction is back on the CPU specialist: the
specialist asks CUAD's canonical convenience question first and, only when that
returns no span and the contract contains explicit cause language
(`material breach`, `uncured`, `termination for cause`, `for cause`), a focused
termination-for-cause follow-up (`specialist/questions.py`,
`specialist/server.py`). Judgment (risk/report) stays on the GPU-bound entries.
The one-pool, small-first and round-robin arms are the same-day §15.8 copies;
the table uses the current config names, while the run directories keep the
retired labels `all-gpu` and `gpu-small`.

| config | wall | extract F1 | risk acc | report acc | fully correct | CPU-served | sensitive approved | violations | GPU-s/doc |
|---|---|---|---|---|---|---|---|---|---|
| one-pool (recorded as all-gpu) | 202.83 s | 1.000 | 0.995 | 0.756 | 199/200 | 0.0% | 0/247 | 247 | 1.013 |
| small-first (recorded as gpu-small) | 454.21 s | 1.000 | 0.395 | 0.278 | 75/200 | 0.0% | 0/247 | 247 | 2.241* |
| round-robin | 570.81 s | 0.991 | 1.000 | 0.756 | 186/200 | 50.0% | 129/247 | 118 | 1.773 |
| **hybrid** | **816.59 s** | **1.000** | **1.000** | **0.752** | **200/200** | **83.3%** | **247/247** | **0** | **0.22** |

Zero errors and zero fallbacks; screen 41/0/159/0; judgment steps 201/201 on
GPU; termination extraction 200/200 on `vllm-cpu`. GPU device time is 44.0 s
(0.22 GPU-s/doc) against 101.6 s (0.508) for the GPU-term route and 202.7 s
(1.013) for one-pool, recorded as `all-gpu`: the CPU policy keeps the
extraction quality of the GPU route at the baseline hybrid's GPU cost, and
wall time is 816.59 s.

The policy was measured twice on the same commit and the same pools. The
first run (`runs/rosa-200-2026-11-09-cpu-term`, archived at `ea80dfb`; wall 823.84 s, 43.4 GPU-s)
scored 198/200 with risk 0.990; the quoted rerun, on a stack rebuilt from
the identical tree (fresh model caches, fresh specialist image build),
scored 200/200 with risk 1.000.

- **The cause follow-up closed the extraction gap.** Extraction is 1.000
  P/R/F1 on all 200 documents with zero false positives: all 48 cause clauses
  are now found (the baseline hybrid missed 27 of them) and all 152 convenience
  clauses still come from the canonical question. The follow-up fired on ~27
  documents, matching the offline prediction; the pod's cumulative
  `clause_extraction_cause` counter (54) also includes ~27 fires from the
  interrupted first attempt and 1 from the live smoke test, which is why it
  reads higher than one run.
- **The first run scored 0.990, not the predicted 1.000 — two borderline
  judgment flips on the GPU MoE — and the rerun confirms they were serving
  variance, not a prompt defect.** Both were sensitive documents.
  `C-2026-0094`: the extraction inputs are byte-identical
  to the §15.8 baseline run where risk scored high (correct); in that run the
  model took the "termination only for material breach → medium" branch over "no
  data-protection clause → high". `C-2026-0187`: the cause follow-up *fixed*
  a missed termination extraction (one of the baseline's 27), and the model
  then answered low, ignoring the material-breach branch that the recovered
  span literally quotes. **A same-prompt, same-inputs replay of all 200 risk
  calls against the GPU pool scores 200/200** — the flips do not reproduce;
  they are temperature-0 kernel divergence under batch load, the same class
  of divergence §14 records between CPU and GPU serving. An ordered
  decision-list rubric ("evaluate in order, stop at the first match") was
  A/B-tested on the same inputs and rejected: no gain on the synthetic corpus
  (200/200 either way) and a CUAD regression (14/19 vs 15/19 on the §17.2
  inputs). one-pool itself scores 0.995 on the same corpus, so 0.990 is within
  the judgment stage's observed variance, not a routing regression — the
  failure moved from extraction (27 documents) to two borderline judgment
  calls. The quoted rerun scored risk 1.000 with 200/200 documents fully
  correct on the same commit, corpus and pools: the flips did not reproduce
  there either, which is the confirmation the variance diagnosis predicted.
- **CUAD held-out quality is preserved by the conditional design.** On the
  19-document CUAD test split the canonical question alone scores termination
  F1 0.714; replacing it outright with a cause or general question regresses
  to 0.667. The conditional strategy (canonical first, cause follow-up only on
  no-span + cause language) scores 0.714 — identical to canonical — because
  the follow-up only fires where the canonical question already returned
  nothing. 200/200 on the synthetic corpus, 0.714 on CUAD: the fix targets the
  synthetic taxonomy gap without disturbing real-data quality.
- Both runs recorded `git_dirty: true`: the routing change was in the working
  tree at first-run time, and the design-doc table edit was uncommitted at
  rerun time.

### 15.10 Route-graph rebuild: work-to-pool in Praxis (2026-11-10, previous record, measured)

The 2026-11-09 runs measured a four-entry policy (general, sensitive, judgment,
sensitive-judgment) with a composite `vllm-cpu` route. On 2026-11-10 the stack
was rebuilt around **two entries and a declarative work-to-pool table**:

- The pipeline chooses only the data-classification entry. The PII screen
  sends personal-data documents to `:8083`; everything else uses `:8080`.
  The app no longer knows pool names, ports, or hardware.
- Inside each entry, the Praxis `router` filter matches `X-Demo-Step`:
  CUAD clause extraction → `cuad-specialist`; classify and
  `extract:data_protection` → `vllm-cpu`; risk/report → `vllm-gpu`
  (general) or `vllm-gpu-approved` (sensitive).
- The general failover set is `vllm-gpu`; a judgment request that is already
  on the GPU retries GPU and then fails, rather than preserving the old
  all-CPU result. The sensitive failover set is `vllm-gpu-approved` only.
- `cuad-specialist` is a pure model endpoint: unsupported prompts return 400.
  There is no `--forward-to` and no second hop outside Praxis.
- The two GPU routes still point at the same `gpumoe-vllm` deployment. The
  split is a policy boundary, not duplicate hardware: fail-closed containment
  is preserved, and `vllm-gpu` can carry non-sensitive GPU work without
  becoming an approved route.

The pre-rebuild 200-document result
(`runs/rosa-200-2026-11-09-cpu-term-rerun/hybrid`) belongs to the old graph:
its `vllm-cpu` count was 600 RoBERTa encoder passes plus 400 forwarded Qwen
calls. It is not evidence for the rebuilt graph.

**Measured 2026-11-10** in
`runs/rosa-200-2026-11-10-praxis-routes/hybrid` (ROSA `<redacted-cluster>`,
200 synthetic documents, 41 sensitive, concurrency 12, `environment: rosa`):
839.9 s, 1,201 steps, 0 errors, 0 fallback, extract F1 1.000, risk 1.000,
report 0.750, 200/200 documents fully correct. The route counts show the
rebuilt table directly:

| stage | route counts |
|---|---|
| classify | `vllm-cpu` 200 |
| extract | `cuad-specialist` 600 (`vllm-cpu` 200 for data protection) |
| risk | `vllm-gpu` 159 (non-sensitive) / `vllm-gpu-approved` 41 (sensitive) |
| report | `vllm-gpu-approved` 1 |

That is 400 `vllm-cpu`, 600 `cuad-specialist`, 159 `vllm-gpu` and 42
`vllm-gpu-approved`; 247/247 sensitive steps were served by the approved set
(`cuad-specialist`, `vllm-cpu`, `vllm-gpu-approved`) and the score reports
**0 sensitive violations**. The app made no pool choice: the pipeline sent
`X-Demo-Step` and the entry only. The same run's controls: one-pool
(recorded as `all-gpu`) 601.2 s with 11 transport timeouts and 245
violations; small-first (recorded as `gpu-small`) 457.6 s with 247
violations; round-robin 938.4 s with 117 violations.

Caveat: device time for the rebuilt hybrid arm is **`n/a`**, not zero. The
per-pool relays did not expose vLLM's in-flight gauges, so the 0.22 GPU-s/doc
figure from the pre-rebuild graph is not a measured claim for this graph. The
run records wall time and route counts; do not price the new arm from the old
cost number. The all-GPU arm's device time was measured (0.043 GPU-s/doc) but
that arm also hit 11 transport timeouts, so it is a control, not a clean
latency baseline. Do not edit or reinterpret the recorded runs.

### 15.11 2026-11-11 re-run: previous four-arm record (measured)

The rebuilt graph was re-measured after the OpenShell sandbox egress fix and
the deterministic report rollup. The 2026-11-11 record is
`runs/rosa-200-2026-11-11-praxis-routes` (ROSA `<redacted-cluster>`, 200
synthetic documents, 41 sensitive, concurrency 12, `environment: rosa`,
regex screen 41/0/159/0):

| config | wall s | extract F1 | risk | report | fully correct | sensitive approved | violations | errors | GPU-s/doc |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| one-pool | 201.89 | 1.000 | 1.000 | 1.000 | 200/200 | 0/247 | 247 | 0 | 0.981 |
| small-first | 489.92 | 1.000 | 0.395 | 0.600 | 77/200 | 0/247 | 247 | 0 | 2.429 |
| round-robin | 935.56 | 1.000 | 1.000 | 0.750 | 200/200 | 137/247 | 110 | 0 | 1.862 |
| **hybrid** | **806.59** | **1.000** | **1.000** | **1.000** | **200/200** | **247/247** | **0** | **0** | **0.265** |

The hybrid arm records 32.555 CPU core-s/doc; round-robin records 36.797.
Routes are unchanged from the rebuilt table: `vllm-cpu` 400,
`cuad-specialist` 600, `vllm-gpu` 159 and `vllm-gpu-approved` 42. Judgment
ran 201/201 on GPU. The report rollup is computed from per-document rows now;
that is why report accuracy is 1.000 here versus 0.750 in the 2026-11-10
record. The one-pool and small-first arms intentionally violate the
sensitive-pool boundary; round-robin has one degraded fallback. All arms have
0 errors.

The 2026-11-11 direct baselines are in `runs/rosa-200-2026-11-11-direct`:
`cpu-moe` 2949.5 s (extract F1 1.000, risk 1.000, report 0.746),
`gpu-moe` 301.6 s (1.000, 1.000, 0.760) and `cpu-small` 1144.4 s (1.000,
0.395, 0.212). The 1.5B CPU pool is not a judgment substitute. The 2026-11-10
matrix remains the previous record; the 2026-11-09 matrix and rerun are
recoverable at `d551c7b` but are no longer in the working tree. This
2026-11-11 matrix remains the last comparable four-arm routing measurement;
the 2026-11-12 RC1 hybrid supersedes it as the quoted record.

### 15.12 2026-11-12 RC1 real-pool validation: current record (measured)

The RC1 image (`quay.io/mtahhan/gci-praxis:v0.9.2`, digest
`sha256:3140a8dad2626ef71504546fa7901f53d9ec68ac49ed20e39919ae0af71c77c6`)
was deployed on real ROSA pools after rebuilding `<redacted-cpu-pool>`
(`x8i.24xlarge`, 96 logical CPUs in 2 NUMA nodes) and `<redacted-gpu-pool>`
(`g5.12xlarge`, 4× A10G), both in `us-east-1a`. The CPU topology script
reproduced the recorded bind lists (`problems: []`) and the live pods
reported `Cpus_allowed_list` 0-15, 24-35 and 16-17 for the MoE, 1.5B and
specialist processes.

Record: `runs/rosa-200-2026-11-12-praxis-routes`, 200 synthetic documents,
41 sensitive, concurrency 12, regex screen 41/0/159/0, entries general 954 /
sensitive 247:

| config | wall s | extract F1 | risk | report | fully correct | sensitive approved | violations | errors | GPU-s/doc |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| one-pool | 157.03 | 0.887 | 0.795 | 0.636 | 159/200 | 0/247 | 1 | 246 | 0.776 |
| small-first | 377.68 | 0.887 | 0.345 | 0.318 | 67/200 | 0/247 | 1 | 246 | 1.856 |
| round-robin | 725.35 | 0.887 | 0.795 | 0.636 | 159/200 | 0/247 | 1 | 246 | 1.890 |
| **hybrid** | **818.87** | **1.000** | **0.995** | **0.998** | **199/200** | **247/247** | **0** | **0** | **0.259** |

The hybrid arm records **33.006 CPU core-s/doc**, 201/201 judgment steps on
GPU, and the same route split as 2026-11-11: `vllm-cpu` 400,
`cuad-specialist` 600, `vllm-gpu` 159, `vllm-gpu-approved` 42. It is 12.3 s
slower than the 2026-11-11 row, with the same containment result and a
0.5-point risk / 0.2-point report difference on one document.

The controls are RC1 boundary evidence, not quality controls. Their configs
have no sensitive listener, so every document arrives at the general entry;
the PII gate returned 246 `403`s per run. One sensitive step in each control
still reached a non-approved route because its request content did not match
a PII pattern. The quality columns above are therefore computed over the 159
non-sensitive documents only (the sensitive rows are 0/41), and **must not**
be compared with the 2026-11-11 controls. This is the concrete reason the
hybrid pipeline must choose the sensitive entry; a content gate alone is not
the containment boundary.

The real-pool 2-document smoke (`runs/rosa-smoke-2026-11-12`) passed with
2/2 documents fully correct, 7/7 sensitive steps approved, 0 violations and
19.17 s wall. The live gate assertions in
`runs/rosa-policy-2026-11-12-real` separately verified keyless 401, unknown
key 401, wrong-step 403, general-entry PII 403, sensitive-entry PII 200,
approved sensitive risk 200, step-less eval 200 and a parsed audit export.
The real-pool resilience run (`runs/rosa-resilience-2026-11-12`) then passed
the parameterized `STACK=rosa` smoke **28/28**, including CPU-to-GPU fallback
with a failed-hop trace and recovery, shared-GPU sensitive fail-closed
(502, no spill), governance (7/7 sensitive-entry traces on approved pools),
audit export, and the policy gate. The two GPU Service names select the same
`gpumoe-vllm` Deployment, so an approved-only outage is not isolatable on
this stack; the test asserts the shared-backend failure instead.

## 16. Open-source reuse audit and adapters (2026-11-05)

This section records the component audit and what was actually implemented.
It is **not** a new quality result: the measured default path is unchanged,
and none of the optional backends has been run on the corpus yet. The point
is to make the next A/B cheap and explicit instead of a silent swap.

### 16.1 Method

The audit used one rule: reuse an upstream where it owns a *mechanic*
(recognition, ranking, offset bookkeeping, storage, export, load generation),
keep our code where the *decision* lives (governance, routing, scoring
definitions, the composite specialist, the audit trail). A candidate was
implemented only behind a flag/extra with a dependency-free or existing
default, because every swap below can move a reported number.

Baseline before the work: 134 tests green (135 observed in the working tree
before the new tests). After: **179 tests green**, of which 33 are new
adapter tests. All optional backends fail with an explicit install/URL error
when absent; none silently changes behaviour.

### 16.2 Adopted adapters

| component | where | default | activation |
|---|---|---|---|
| Presidio | `hybrid_demo/pii.py` | regex | `HYBRID_PII_DETECTOR=presidio` or `composite` |
| llm-d-sc taxonomy (HTTP bridge) | `hybrid_demo/pii.py` | off | `HYBRID_PII_DETECTOR=taxonomy`, `HYBRID_PII_TAXONOMY_URL` |
| nervaluate | `hybrid_demo/evalkit/spans.py` | containment only | install `spans` extra; offsets appear when doc text is available |
| statsmodels | `hybrid_demo/evalkit/stats.py` | exact binomial | install `stats` extra; `--backend auto` |
| ranx | `hybrid_demo/retrieval/metrics.py` | internal | `--metrics-backend ranx` |
| docling `HybridChunker` | `hybrid_demo/retrieval/docling_chunking.py` | builtin chunker | `--chunk-backend docling`, `ingest` extra |
| LanceDB + BM25 | `hybrid_demo/retrieval/index.py` | memory | `hybrid-index --backend lancedb` |
| RedisVL + Redis | `hybrid_demo/specialist/cache.py` | in-memory | `--cache-backend redis`, `cache` extra |
| OpenTelemetry | `hybrid_demo/traceui/otel.py` | off | `TRACE_OTEL=jsonl` or `otlp` |
| RAGAS | `hybrid_demo/evalkit/rag.py` | lexical screen | `rag` extra + `ragas_scores(...)` |
| llm-d-inference-sim | `hybrid_demo/fakepool/llmd_sim.py` | `hybrid-pool` | `hybrid-pool-sim`, then point the pool env at it |
| llm-d-batch-gateway | `hybrid_demo/pipeline/batch.py` | Python runner | `hybrid-batch --gateway <praxis ingress>` |
| vLLM bench / GuideLLM | `hybrid_demo/bench.py` | `tools/gpu_bench.py` | `hybrid-bench --backend vllm|guidellm` |

Also changed additively: `extraction_scores` now carries `by_mode`
(exact/containment/partial), `absence`, and nervaluate's offset strategies;
the report table still prints containment as the primary number. `score.py`
loads `data/<set>/documents/*.txt` to supply offsets. `review.py` can attach
JSON schemas to request bodies (`HYBRID_STRUCTURED_OUTPUTS`), off by default.
`traceui` writes the OTel span after the SQLite row, in a try/except that
cannot break the relay.

### 16.3 Findings

1. **The PII screen is a routing input, not a text filter.** On the 200
   synthetic documents, the regex default flags 41 (20.5%) with 164 findings;
   that is the measured sensitive share. Presidio adds NER (persons,
   locations, organisations, checksum-backed identifiers) but there is no
   CUAD PII label, so its precision/recall cannot be quoted from this corpus.
   Presidio will likely flag company names and addresses in contracts; that
   is a routing change, not a bug. The comparison tool therefore reports
   document flags/findings always, and TP/FP/TN/FN only where a manifest
   supplies a label.
2. **Containment was hiding boundary behaviour.** Exact equality and token-
   Jaccard views are cheap to add and show the difference; nervaluate adds
   the four SemEval strategies once document text is available. But offset
   scoring requires the predicted quote to be locatable in the document. A
   paraphrase that containment accepts will be counted as missed by
   nervaluate. That is the honest reading, not a reason to switch the primary
   metric.
3. **MRR and NDCG answer different questions.** MRR stops at the first
   relevant chunk; NDCG@10 credits all relevant chunks by position. The
   evaluator now reports both (plus MAP) with the backend that produced them,
   so a change in ranking quality cannot hide behind recall@1.
4. **docling provenance needs corroboration.** A merged chunk can carry the
   charspan of its first item, and the converter's exported text may not be
   the text the charspan indexes. The adapter checks that the slice supports
   the chunk text and relocates by search when it does not; the fallback
   increments a visible `relocated` count. This is the D11 bug class, so the
   check is deliberately in the path rather than in a test-only helper.
5. **`llm-d-inference-sim` is the right transport simulator, not a replacement
   for the cost/corruption fake.** It supports OpenAI chat/completions,
   streaming, latency and load, KV/prefix-cache metrics and HTTP failure
   injection. Its failure types are transport/API errors
   (`rate_limit`, `server_error`, ...), not the demo's deterministic
   complexity-tier answer corruption, and it does not emit our
   `X-Busy-Ms` device time. The proxy therefore exports
   `X-Proxy-Busy-Ms`/`X-Proxy-Ttft-Ms` and leaves `X-Busy-Ms` absent by
   default. Using proxy wall time as device time would silently move the
   cost report; `LLMD_SIM_DEVICE_TIME_HEADERS=1` exists only for a run that
   is labelled accordingly.
6. **Batch is a transport, not the measurement runner.** The
   `/v1/files` + `/v1/batches` client is useful for portfolio-scale
   submission, but the Python runner still owns per-document records, route
   headers and arm comparison. Point the client at the praxis ingress; the
   gateway's native path routes to the llm-d router, so using it directly
   would bypass the governance path the demo measures.
7. **Observability must stay additive.** SQLite remains the audit trail; OTel
   spans use GenAI semantic-convention names plus `app.*` routing fields, and
   never export sensitive document text. Prompt tokens are estimated from
   the question unless the upstream sends `x-prompt-tokens`; the span carries
   `gen_ai.usage.input_tokens_estimated` so a reader can tell.
8. **Do not delegate the cache policy.** RedisVL supplies the vector index and
   eviction; the wrapper still enforces exact-before-similarity, sensitive
   skip/store refusal, and one clause filter. Redis is also a licensing
   decision: Redis 7.4 is RSALv2/SSPL and Redis 8 adds an AGPLv3 option;
   RedisVL itself is Apache-2.0. Valkey is a drop-in alternative if those
   terms are unacceptable.
9. **Judge metrics need a deterministic screen too.** RAGAS faithfulness and
   context precision depend on the judge model, so their numbers are only
   comparable across runs with the same judge. The lexical groundedness /
   context-precision screen is crude but deterministic and free; it catches
   unsupported report reasons before a judge is involved.
10. **Benchmark CLIs churn.** `vllm bench serve` no longer accepts
    `--output-json`; current releases use `--save-result` with
    `--result-dir`/`--result-filename`. GuideLLM v0.7+ replaced
    `benchmark run --target` with `run --backend kind=openai_http,target=...`
    and `kind=` specs for profile/data/constraint. The wrappers build the
    current forms and expose `--extra`; pin versions in a measured run.
11. **Structured outputs are a quality experiment, not a cleanup.** The wire
    format changed (`structured_outputs.json` current, `guided_json` older,
    `response_format` OpenAI-compatible), and constraining decoding can
    change answer length and quality. Default is off; schemas are permissive
    about extra keys.

### 16.4 Verification

- `cd demo && .venv/bin/python -m pytest -q` → **179 passed**.
- `python -m hybrid_demo.pii` → `{regex: true, presidio: false, taxonomy: false}`
  in the base environment.
- `python tools/compare_pii.py --data data/synthetic --detectors regex` →
  41/200 flagged, 164 findings.
- `python -m hybrid_demo.bench ... --dry-run` → current vLLM and GuideLLM
  command lines (asserted in tests).
- `python -m hybrid_demo.fakepool.llmd_sim ... --print-simulator-cmd` →
  `llm-d-inference-sim --model ... --port ... --mode random`.
- OTel JSONL round-trip tested, including the sensitive/no-content path.

Not measured yet: Presidio on a labelled sample, docling chunk/offset parity on
the D11 regression set, ranx vs internal NDCG/MAP on CUAD, Redis hit-rate
under multi-replica load, RAGAS against the demo's judge, OTLP to a live
collector, and any GPU throughput from the new bench wrappers.

### 16.5 Next measured actions

1. Label ~100 synthetic/sensitive documents (or use the PII drills) and run
   `tools/compare_pii.py --detectors regex,presidio`; adopt only if the
   containment/routing trade is understood.
2. Run `hybrid-retrieval-eval --chunk-backend docling --metrics-backend
   ranx` against the D11 set; compare offsets and NDCG/MAP to the builtin
   baseline, not just recall.
3. Run the four arms with Presidio and, separately, with structured outputs
   on; report the sensitive share, CPU/GPU split, F1 and cost side by side.
4. If a shared cache is needed, run two specialist replicas against Redis and
   record exact/similarity hit rates and false positives; keep the
   in-memory backend as the single-replica default.
5. Point `hybrid-bench` at the real pools and compare against
   `tools/gpu_bench.py`; record the CLI version in the result metadata.

### 16.6 Rejected as replacements

- Replacing the trace relay/dashboard with Langfuse/Phoenix: the relay is also
  the ingress, recorder and audit export, and its SQLite rows are read by the
  governance check. OTel export is additive; Langfuse (MIT) is the preferred
  dashboard if one is published, Phoenix's ELv2 is a licensing caveat.
- Replacing `pipeline/review.py` / `evalkit/score.py` logic with a framework:
  the step prompts, rubric and scoring definitions are the domain, and they
  are what the numbers mean.
- Replacing the composite specialist or its cache policy with an agent
  framework: the composition is a config-level routing decision we measure.
- Running batches directly through `llm-d-batch-gateway` without the praxis
  ingress: it would bypass the governance path.
- Treating `common.embed` as a production embedder: the 256-dim char-trigram
  hash is a stand-in; `litil-embed-0.6b` remains the measured candidate.

### 16.7 Jev/Laya PII screen, offline (2026-11-11) **[measured offline only, not a serving arm]**

The `jev` detector in `pii.py` speaks the System One request/answer shape and
asks one document-level `noul` question. It can call a served TypeSafe Jev
endpoint (`HYBRID_JEV_URL`) or run the open-source Laya checkpoint locally
(`jev` extra, `HYBRID_JEV_MODEL=auto|english|multilingual`). The question is
deliberately about **actual identifiers**, not about the presence of a
data-protection clause:

> Does this text contain PII such as an email address, phone number, employee
> ID, or a named individual's contact details?

Command:

```bash
cd demo
uv run hybrid-jev-bench --data data/synthetic --json /tmp/jev-bench.json
```

This is a **weights** measurement on the 200-document synthetic corpus
(41 sensitive), local CPU, `laya==0.4.31`, one document at a time. It does
not exercise Praxis, the gateway, the pools or the trace UI, and it is not a
throughput or cost claim.

| screen | threshold | tp | fp | tn | fn | precision | recall | accuracy |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| regex (measured default) | — | 41 | 0 | 159 | 0 | 1.000 | 1.000 | 1.000 |
| Laya English | 0.5 | 40 | 0 | 159 | 1 | 1.000 | 0.976 | 0.995 |
| Laya English | 0.4 | 41 | 0 | 159 | 0 | 1.000 | 1.000 | 1.000 |
| Laya English | 0.6 | 30 | 0 | 159 | 11 | 1.000 | 0.732 | 0.945 |
| Laya English | 0.7 | 9 | 0 | 159 | 32 | 1.000 | 0.220 | 0.840 |

The one miss at 0.5 is `C-2026-0139`, at 0.422; the highest non-sensitive
score is 0.094. The 0.4 threshold recovers it with no false positives, but
0.4 was chosen after seeing this corpus, so it is not evidence of a better
calibrated threshold. 0.5 is the adapter default; a deployment would calibrate
on a held-out real sample.

Three findings:

1. **The regex wins on the only labelled corpus.** It is 41/0/159/0 on the
   synthetic set, so Jev does not beat it there. The claim for Jev is the
   no-labels case — real contracts and CUAD have no PII labels — and that is
   still unmeasured. This is why the default did not change and no run
   directory was created.
2. **Question wording dominates the result.** Asking "does this text contain
   a data-protection clause?" produced **72 false positives**: 148/200
   synthetic contracts carry a generic data-protection clause but only 41 are
   labelled sensitive. Asking for actual identifiers produced zero false
   positives. A zero-shot model needs the decision defined, not just a topic.
3. **The checkpoint matters more than the threshold.** The multilingual
   checkpoint on the same identifier question flagged 141/159 non-sensitive
   documents at 0.5. The English checkpoint is the right default for English
   text, and `auto` selects it via `laya.is_english`. Non-English contracts
   remain unmeasured; the multilingual path is available but should not be
   quoted as validated.

The adapter is opt-in: `HYBRID_PII_DETECTOR=jev`. A failure raises and
`is_sensitive()` fails closed to the sensitive entry, the same invariant as
the other detectors. One operational caveat: `detect_sensitivity` is also
called by the specialist cache, so a Jev screen is a model call per screened
context, not once per document. A served deployment should cache the
document-level verdict or screen once at ingest; this adapter does neither. `composite` still means regex OR Presidio OR taxonomy;
Jev is deliberately not included, because selecting it may download and load
a checkpoint. The served `jev-cpu` pool and the `screen:pii` route are
implemented and exercised on the fake stack (§16.9); they are opt-in and have
not been measured as a serving arm, so this section does not change any
recorded number. **Measured as a serving arm on 2026-11-13: see 16.7.1.**

### 16.7.1 The Jev screen as a serving arm (2026-11-13) **[measured]**

`runs/rosa-200-2026-11-13-jev-screen/hybrid`: the 200-document synthetic
matrix with `HYBRID_PII_DETECTOR=screen` and
`HYBRID_JEV_THRESHOLD=0.4`, so the pipeline's per-document screen call runs
through the authenticated `:8086` listener, praxis, the `jev-cpu` relay and
the Laya service - an audited, routed hop instead of a client-side regex
(RC1 `v0.9.2` stack, real pools, concurrency 12, `SAMPLE_JEV=1`).

| | jev screen (serving) | regex (RC1 record) |
|---|---|---|
| screen tp/fp/tn/fn | **41/0/159/0** | 41/0/159/0 |
| sensitive approved / violations | 247/247, 0 | 247/247, 0 |
| judgment on GPU | 201/201 | 201/201 |
| extract F1 / risk / report | 1.000 / 1.000 / 1.000, **200/200** | 1.000 / 0.995 / 0.998, 199/200 |
| wall | 981.33 s | 818.87 s |
| screen device time | 157.5 s `jev-cpu` active (123.1 s request time) | - |

- **The offline sweep held in serving exactly**: at threshold 0.4 the served
  decisions are 41/0/159/0, identical to the regex screen and to 16.7's
  local Laya run. The default 0.5 would have missed one document (16.7);
  the sweep is why the run set 0.4.
- **Governance and quality are unchanged** - 0 violations, identical route
  counts, and quality at-or-above the RC1 record within temperature-0
  variance (this run's judgment stage had no flips; the RC1 run had one).
- **The cost is measured, not guessed**: +162 s wall (~20%) and ~0.79 s of
  `jev-cpu` device time per screened document, plus one more pool to run.
  The regex screen is free and client-side.
- **Adoption stays opt-in.** Regex wins on the only labelled corpus (both
  are perfect there, and regex costs nothing); the Jev screen's claim
  remains the no-labels case - real contracts and CUAD have no PII
  annotations - and it is now a *serving-viable* option for that case with
  its cost on record. The default detector does not change.
- Bring-up gotcha, fixed the same day: the praxis Service did not expose
  the `:8086` listener port, so the trace-ui screen relay hung and the
  pipeline's screen failed closed (every document sensitive). The listener
  worked on the fake stack but had never been called on ROSA. The Service
  and container now declare the `screen` port (commit `9d2d4bc`).

### 16.8 The cause-language trigger: Jev fails the CUAD guardrail (2026-11-11) **[measured offline only]**

The specialist's conditional termination follow-up is gated by
`cause_language_present()` in `specialist/questions.py`. The measured
default remains the narrow regex (`material breach`, `uncured`, `termination
for cause`, `for cause`). An opt-in Jev decision (`HYBRID_CAUSE_DECIDER=jev`)
asks whether the contract allows termination for cause and uses the same
chunked System One client; a serving/model failure falls back to the regex
with a one-time warning.

The adoption gate is the recorded CUAD termination F1 of **0.714** on the
19-document test subset. Command:

```bash
cd demo
uv run hybrid-cause-bench --deciders regex jev --thresholds 0.4 0.5 0.6 \
    --json /tmp/cause-bench.json
```

| trigger | threshold | tp | fp | fn | precision | recall | F1 | passes |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| regex (measured default) | — | 5 | 4 | 0 | 0.556 | 1.000 | **0.714** | yes |
| Jev | 0.4 | 5 | 6 | 0 | 0.455 | 1.000 | 0.625 | no |
| Jev | 0.5 | 5 | 6 | 0 | 0.455 | 1.000 | 0.625 | no |
| Jev | 0.6 | 5 | 6 | 0 | 0.455 | 1.000 | 0.625 | no |

The Jev trigger finds all five cause clauses but fires on two annotated-negative
contracts the regex leaves alone (the max-over-chunks score is high on
non-cause termination language). It therefore **fails the guardrail and is
not adopted**; the regex stays the default. The opt-in code remains so the
decision can be re-measured on a larger or real sample. This is a deliberately
conservative outcome: the trigger changes the specialist's answer only where
the canonical convenience question found nothing, and the guardrail forbids
trading real-data precision for a synthetic-corpus gain.

### 16.9 Pre-entry PII screen through Praxis (`screen:pii` -> `jev-cpu`) (2026-11-11)

The PII screen runs **before** the pipeline can choose the general or
sensitive review entry, so it cannot use either review listener. The stack now
has a third, control-plane listener:

```
pipeline -> trace-ui screen ingress :8086 -> Praxis screen listener :8086
         -> trace-ui jev-cpu relay :8089 -> jev-cpu (Jev/Laya System One)
         -> Decode -> pipeline chooses general :8080 or sensitive :8083
```

- `manifests/base/praxis/config.yaml` adds the `screen-gateway` listener,
  the `screen-routing` chain, and the `jev-cpu` cluster. The chain accepts
  `X-Demo-Step: screen:pii` (and the optional `decide:cause`) and pins both to
  `jev-cpu`; there is no failover. An unavailable screen is a hard error, and
  `is_sensitive()` fails closed to the sensitive entry.
- `pii.screen_detect` / `HYBRID_PII_DETECTOR=screen` sends the System One body
  with `X-Demo-Step: screen:pii`, the new `screen` API key, and the served
  model name (only so the gateway's model-scoped policy route matches). The
  service ignores the model field. Set `HYBRID_PII_SCREEN_URL` to the screen
  ingress's `/v1/systemone`.
- `demo/src/hybrid_demo/jev/server.py` is the `jev-cpu` endpoint: `/health`,
  `/v1/models`, `/metrics`, and `/v1/systemone` for `noul` questions. It
  serialises Laya calls, returns `X-Busy-Ms`, and rejects unsupported shapes
  with 400. `Dockerfile.jev` bakes the English checkpoint.
- The trace UI records screen calls with `entry=screen` and route `jev-cpu`;
  they are badged `SCREEN` in the table and deliberately omitted from the
  document-review flow diagram, because they are not a review entry. The
  static diagrams carry a control-plane note. The screen body is the
  document text, so `entry=screen` is treated like a sensitive entry for
  content storage: the default trace DB keeps the routing metadata but not
  the body.
- `deploy/linux/docker-compose.yml` has `fake-jev` for the fake profile and
  `jev-cpu-real` (the baked image) for `up.sh --real`; the ROSA overlay adds
  `deploy/openshift/rosa/jev-cpu.yaml` and the trace-ui ports/env.

Verification: `248 passed` at the 2026-11-12 tree (the screen/cause tests
cover the detector's headers/body, fail-closed behavior, the service contract,
the cause guardrail's scoring, the config's third listener, and a fake-stack
`screen:pii` end-to-end call; RC1 adds the step-key and PII-gate policy
tests). The screen route is **not** in a recorded run and changes no measured
number: the default screen remains regex and no run directory was created. A
cluster deployment still requires rebuilding the stack and the `hybrid-jev`
image.

## 17. The CUAD risk gap is an input gap (2026-11-09)

The question (§4 of the 2026-11-09 handoff): the CUAD test-split risk accuracy
of the 35B-A3B — 0.632 on both the direct CPU and direct GPU runs — sits below
the majority-class baseline (0.684, "always answer high"). Is that a
model/prompt/rubric problem?

### 17.1 Rule-following is perfect; the inputs are wrong

Method: replay the shared rubric (`pipeline/risk.assess_risk`) over each
run's *own extracted clauses* and compare with the model's recorded risk
answer, per document, on the two recorded direct runs
(`runs/cuad-test/arm-direct-moe-gpu-v2`, `arm-direct-cpu-moe-v2`).

- GPU direct: risk answer = rubric(own extractions) on **19/19 documents**.
- CPU direct: **19/19** — identical on both hardware paths.

The model applies the rubric exactly to the clause set it is given. Every
error is an input error. Decomposition of the seven misses (of 19):

| document | truth | model | cause |
|---|---|---|---|
| HPILHOLDING | low | high | liability cap missed by the MoE's extraction (CUAD annotates one) |
| PLAYAHOTELS | medium | high | liability cap missed |
| MSCIINC | medium | high | liability cap missed |
| FEDERATED | high | low | MoE answered with a cap ("capped to the assets of the Fund"); CUAD annotates none |
| CURAEGISTECHNOLOGIES | medium | low | false-positive termination (CUAD annotates no convenience clause) |
| TRIZETTO | medium | low | false-positive termination |
| PAXMEDICA | high | medium | taxonomy: truth derives from CUAD's "Uncapped Liability" annotation — a fifth category no pipeline stage extracts |

Six of seven errors are extraction quality feeding the judgment stage; one is
a truth-construction gap (the mirror image of the known "CUAD has no
data-protection category" gap). The risk prompt is not the problem, and
changing it cannot help: the model already follows it perfectly.

### 17.2 The hybrid pipeline is the fix: specialist extraction (measured)

`runs/cuad-test/arm-direct-specialist-rosa` (2026-11-09, ROSA): the direct arm
against the specialist endpoint, so extraction is the CUAD-trained checkpoint
on all four clause keys and judgment (classify/risk/report) is forwarded to
the 35B MoE — the path with measured CPU/GPU judgment parity (§14). 19
documents, 912.6 s wall, concurrency 4, no gateway in the path (labelled
direct; it is a model-quality measurement, not a governance claim).

Extraction per clause (containment, vs the CUAD test-split annotations):

| clause | P | R | F1 | notes |
|---|---:|---:|---:|---|
| governing_law | 1.000 | 1.000 | 1.000 | perfect |
| audit_rights | 1.000 | 1.000 | 1.000 | perfect |
| termination | 0.556 | 1.000 | 0.714 | all 5 truths found; the 4 FPs are the same annotated-negative docs the canonical question already FPs on — the cause follow-up added none |
| liability_cap | 0.333 | 0.286 | 0.308 | CUAD's hardest category; the checkpoint misses annotated caps (HPILHOLDING, PLAYAHOTELS, MSCIINC, PAXMEDICA, TRIZETTO) the MoE finds |

Risk: **14/19 = 0.737** measured (one answer unparseable — Scansource
rambled past the JSON contract), against **0.632** for the direct MoE
extraction and the **0.684** majority baseline. A same-input replay of the
19 risk prompts on the GPU MoE scores **15/19 = 0.789** (the unparseable
answer parses and is correct), consistent with §14's CPU/GPU judgment parity.
Specialist extraction recovers HPILHOLDING, PLAYAHOTELS-partly, MSCIINC and
FEDERATED; what remains wrong is the cap-category weakness above, four
termination FPs, and the PAXMEDICA taxonomy gap (§17.4).

Judgment fidelity held: the MoE's answers track a found/absent replay of the
rubric on its own extractions on 14/19, and on **four documents the model was
right where the found/absent replay was wrong** — it read the clause *text*
(e.g. answered high on a "fully liable, without limitation" span the
specialist had filed under liability_cap). The rubric asks for text reading
("uncapped"), not presence counting; the model does the former.

Direct-vs-hybrid per stage: the MoE is the better *cap* extractor on CUAD and
the specialist is the better termination/governing-law/audit extractor; risk
quality follows whichever inputs each stage receives. A clause-level split
(caps to the MoE, the rest to the specialist) was the obvious next
experiment; §17.5 measures it and rejects it.

### 17.3 The through-stack CUAD path is timeout-bound (finding)

The first attempt to measure this end-to-end through the ROSA stack
(`runs/cuad-test-hybrid-2026-11-09`, archived at `ea80dfb`) hit a structural limit and **must not be
quoted as a policy result**:

- The specialist pod (2 pinned cores, `--concurrency 4`) answers a
  ~6k-token CUAD contract in 19–295 s under load. Praxis's
  `step_timeout_ms: 120000` aborts the slower calls, and after consecutive
  failures the subrequest circuit breaker opens for the `vllm-cpu` peer
  (`sub-request circuit open for peer …:8081` in the praxis log).
- 34 of 80 extraction steps were then served by the 35B MoE through the
  failover sets — 16 on `vllm-gpu` via `vllm-any` (general entry), 18 on
  `vllm-gpu-approved` (sensitive entry) — and 15 failed closed with 503,
  including aborts at exactly 120.002 s.
- **The run summary counted `fallback: 0` while 34 steps were degraded.**
  Praxis does not emit `X-Praxis-Fallback` on proxied failovers (§7) and the
  classifier-label mismatch signal left with the classifier; the trace-UI's
  errored-hop inference does mark these requests. The pipeline's run summary
  needs the same inference (or an entry-vs-route pin check) before a degraded
  run can be disclosed by its summary alone.
- The resulting mixture — 31 specialist answers, 34 MoE answers, 15 failed
  extractions — scored risk 0.65 and extraction F1 0.692: not interpretable
  as either policy.
- The same shape appears in the SNO-era `runs/cuad-test/arm-D-specialist`
  (21 errors, 337 s average latency, 2,543 s wall): the
  specialist-through-gateway path on long real contracts has always been
  timeout-bound. This is not a ROSA regression.

Options, not implemented today: raise `step_timeout_ms` for long-document
workloads (the pipeline client and route timeout are already 600 s), give the
specialist more cores or a higher semaphore, or keep CUAD quality
measurements on the direct path. Until one of those is chosen and measured,
the direct-specialist measurement (§17.2) is the honest number.

### 17.4 What remains

PAXMEDICA-type documents stay wrong under any four-key extraction: CUAD's
"Uncapped Liability" annotation feeds the truth rule but no pipeline stage
extracts that category. Fixing it means either extracting a fifth clause
category — which changes the CUAD manifest shape and breaks comparability
with every recorded run — or documenting the gap. That is the same choice the
data-protection category already faced, and documenting it is the honest
option for now.

### 17.5 The clause-level cap split, measured and rejected (2026-11-13)

The §17.2 follow-up, run through the stack on the rebuilt graph
(`runs/cuad-2026-11-13/`, RC1 `v0.9.2`, same session, serial arms,
concurrency 4, 20 manifest entries / 19 unique documents). Config E
(`cap-split.yaml`) routes `extract:liability_cap` to `vllm-cpu` in both
review listeners; everything else is the hybrid policy.

The stacked hybrid baseline is now **valid** — the §17.3 timeout problem is
fixed by the 600 s step deadline: 875.7 s, 0 errors, 0 fallbacks, 0
violations, all 80 extraction steps on `cuad-specialist`, judgment 21/21 on
GPU. Its quality replicates the direct-specialist run of §17.2 exactly
(termination 0.714, governing law and audit rights 1.000, caps 0.308, risk
14/19 = 0.737, the same five misses): **the gateway adds no quality loss on
CUAD.**

The split:

| | hybrid (specialist caps) | cap-split (MoE caps) |
|---|---:|---:|
| wall | 875.7 s | 665.98 s |
| liability_cap P / R / F1 | 0.333 / 0.286 / 0.308 | 0.667 / 0.286 / **0.400** |
| risk accuracy | **14/19 = 0.737** | 13/19 = 0.684 |
| errors / fallbacks / violations | 0 / 0 / 0 | 0 / 0 / 0 |

**Rejected.** The cap F1 gain is precision-only — recall is 2 TP / 5 FN on
both routes, because the MoE misses the same annotated caps it missed in the
direct arm — and a precision-only gain does not convert to risk accuracy at
this corpus size (7 of 19 documents have an annotated cap). At the document
level the split recovers Xinhua (the specialist's cap false positive there
disappears) but loses MSCIINC and HPILHOLDING (the MoE's cap misses flip
them to high): net −1, landing exactly on the majority-class baseline. In
both arms the model's risk answers equal a rubric replay over its own
extractions on every wrong document — the judgment stage stayed perfectly
faithful; the losses are pure extraction. The 24 % wall-time saving is real
(the 16-core MoE answers caps faster than the 2-core specialist) but quality
rules, and on the synthetic corpus the specialist's caps are perfect (F1
1.000 in every recorded 200-document matrix), so the split buys nothing
there either. `config.yaml` stays the hybrid policy; config E remains in the
catalog as the recorded alternative.

## 18. RC1 gateway hardening: PII gate and step-key binding

This section records the RC1 implementation, local tests, real-pool
validation, and the functional fake-pool smoke. The quoted 200-document
record is now §15.12; §15.11 remains the previous four-arm matrix.

Two PPE plugins were added to the policy host in RC1. Image `v0.9.2`
(published 2026-11-12, run `43203547413`, digest
`sha256:3140a8dad2626ef71504546fa7901f53d9ec68ac49ed20e39919ae0af71c77c6`)
registers both; it is the image behind the 2026-11-12 real-pool record. The
2026-11-11 record ran `v0.9.1`, which predates them.

- `validator/gci-step-key` (`mode: sequential`) reads the authenticated
  subject and `X-Demo-Step` and requires a `step:<stage>` role matching the
  header's stage. The key directory grants classify, extract, risk, report,
  and screen/decide individually; the eval key is step-less and may only
  make a request with no step header. A valid extract key claiming `risk`
  is denied.
- `validator/gci-pii-gate` (`mode: sequential`) is the same pattern catalog
  as the client screen and the existing observe plugin, but returns a 403
  `pii_detected` violation on a match. It runs on the general review entry.
  The screen listener moved to `policy-screen.yaml` and the sensitive entry
  keeps `validator/gci-pii-observe`, both audit-only: screening and sensitive
  review must be allowed to see the document.

Policy shape: the general route runs `step-key`, then `pii-gate`, then
`audit`; the sensitive route runs `step-key`, taints the session, then
`pii-observe` and `audit`; the screen route runs `step-key`, then
`pii-observe` and `audit`. `validate-policy.yaml` names all four registered
kinds so an image missing one fails its build-time validation. The
`step-key` and `audit` plugins must declare `read_roles` in addition to
`read_subject`: the filter projects only the subject id for `read_subject`,
so without `read_roles` the gate sees an empty role set and denies every
keyed request (`step_key_mismatch`).

Local and live evidence:

- `cd demo && uv run pytest -q` -> **248 passed**, including structural
  tests that pin the policy runs, the screen listener's policy path, the
  key digests, each key's step grants, and the `read_roles` capability.
- `bash -n scripts/linux/smoke.sh scripts/cluster/smoke.sh` -> clean. Both
  smoke tests send the per-step key and assert: a classify key claiming
  risk is denied, a PII-positive general request is denied with 403, the
  sensitive entry still accepts the matching key, the step-less eval
  identity is accepted, and the logs contain both observe and gate scanner
  lines plus a gate denial. The cluster smoke's POST helper deliberately
  does not use `curl -f`, so a fail-closed 4xx/5xx reaches the assertion.
- Published `v0.9.2` passed build-time `praxis-ai --validate` with all four
  plugin kinds. On ROSA, `deploy/openshift/fake-all` (five simulated pools)
  ran `scripts/cluster/smoke.sh`: **27/27 checks passed** (2026-11-12,
  `runs/rosa-policy-2026-11-12`). This includes the wrong-step denial, the
  PII gate, sensitive-pool containment, fail-closed pool loss, and the
  step-less eval request. No model-quality or cost number comes from this
  fake-pool stack.
- Real-pool ROSA validation (2026-11-12), after rebuilding the `x8i.24xlarge`
  and `g5.12xlarge` machine pools in `us-east-1a`:
  `runs/rosa-smoke-2026-11-12` passed the two-document hybrid smoke (2/2
  correct, 7/7 sensitive steps approved, 0 violations, 19.17 s);
  `runs/rosa-policy-2026-11-12-real` verified keyless 401, unknown key 401,
  wrong-step 403, general-entry PII 403, sensitive-entry PII 200, approved
  sensitive risk 200, step-less eval 200 and a parsed audit export;
  `runs/rosa-resilience-2026-11-12` ran the parameterized `STACK=rosa` smoke
  **28/28** (CPU fallback/recovery, shared-GPU sensitive fail-closed with
  502/no spill, governance, audit and policy gate); and
  `runs/rosa-200-2026-11-12-praxis-routes` recorded the 200-document matrix.
- The real-pool hybrid row is **818.87 s**, extract F1 **1.000**, risk
  **0.995**, report **0.998**, **199/200** fully correct, **247/247**
  sensitive steps approved, **0** violations, **0** errors, **0.259
  GPU-s/doc** and **33.006 CPU core-s/doc** (§15.12). This is the current
  quoted RC1 record.

The real-pool controls are not comparable quality rows. All three send every
document to the general entry, where the PII gate denied 246 sensitive steps
and one content miss per run reached a non-approved route. That is the
boundary the hybrid's sensitive entry exists to enforce; the 2026-11-11
`v0.9.1` matrix remains the last comparable four-arm routing measurement.
The earlier fake-all smoke remains functional evidence only.

## 19. V19 measured: the sensitivity decision inside one gateway chain (2026-11-13)

Config F (`single-entry.yaml`), image `v0.9.3` (internal-registry digest
`sha256:d69493b7…` — the quay publish of v0.9.3 is pending a fresh quay
token; v0.9.2 cannot load this config, correctly). One review listener
(:8080) serves every document; the `gci_screen` HTTP filter
(`deploy/images/praxis-llm-d-sc/gci-screen-filter/`, unit-tested 8/8
against praxis-filter 0.7.5) scans each request body with the shared
thirteen-pattern PII catalog, strips any client-supplied
`X-Demo-Screen`, and sets the verdict itself — fail closed on an
unparseable body. A plain `router` + `load_balancer` pair in the same
chain matches `X-Demo-Step` × `X-Demo-Screen`; sensitive judgment work
pins to `vllm-gpu-approved`.

### 19.1 Three engine constraints found the hard way

1. **The iterative_request_router cannot consume an outer-chain filter's
   header.** The IRR builds each step's sub-request from the headers as
   received (`original_request` in the IRR source), so a pending header
   set by a chain-level filter never reaches the nested router: the
   screen-constrained routes silently fell through to the plain
   `X-Demo-Step` routes. In one chain the router's `PendingRouteHeaders`
   overlay sees the filter's set — that is the mechanism's documented
   purpose, and the single-entry chain uses it.
2. **A body-access filter inside an IRR step hangs the step executor**
   before any filter logs (gci_screen in the route step; requests
   deadlocked with zero log lines).
3. **At chain level the router and load balancer must be paired**, as the
   IRR steps pair them: the router alone resolves the cluster and the
   proxy fails with "no upstream selected …; is a load_balancer
   configured?".

Stated trade-off: without the IRR there are no failover steps. A failed
pool fails the request outright — fail closed, never a spill — where the
two-entry graph degrades general work onto `vllm-gpu` and fails sensitive
work closed onto the approved pool. The single-entry config therefore has
different resilience characteristics from the RC1 record; its governance
and quality are what it exists to measure.

### 19.2 The forge test

A risk-step request with a PII body and a **client-forged**
`X-Demo-Screen: general` routes to `vllm-gpu-approved` (the gateway's own
scan overrides the forgery); a clean risk step routes to `vllm-gpu`; an
eval key claiming a review step is denied 403 by the step-key binding. A
client can no longer put personal data on a general pool by lying — the
pre-RC1 trust model ("the pipeline's screen picks the entry") is replaced
by a decision the client cannot influence.

### 19.3 The 200-document measurement

`runs/rosa-200-2026-11-13-single-entry/single-entry` vs the RC1 record
(`runs/rosa-200-2026-11-12-praxis-routes/hybrid`), same stack, same
corpus, concurrency 12:

| | single-entry (V19) | two-entry (RC1 record) |
|---|---|---|
| wall | 809.88 s | 818.87 s |
| errors / fallback | 0 / 0 | 0 / 0 |
| screen tp/fp/tn/fn | 41/0/159/0 | 41/0/159/0 |
| extract F1 / risk / report | 1.000 / 1.000 / 1.000, **200/200** | 1.000 / 0.995 / 0.998, 199/200 |
| risk routes | 159 `vllm-gpu` + 41 approved | 159 + 41 (identical) |
| sensitive steps approved | **246/247, 1 violation** | 247/247, 0 |
| judgment on GPU | 201/201 | 201/201 |
| GPU-s/doc · CPU core-s/doc | 0.258 · 32.533 | 0.259 · 33.006 |

Quality is at-or-above the record within temperature-0 variance (this
run's judgment stage had no flips). The risk-route split is **exactly**
the two-entry split: on every per-document step the gateway's body-level
screen and the pipeline's document-level screen agree.

### 19.4 The one disagreement is the finding

The single violation is the **report step**: the pipeline's flag-based
counting calls it sensitive (some document screened sensitive), but the
report's aggregated body matches **zero of the thirteen PII patterns**
(verified against the recorded request body: the report lines carry refs,
types, risk labels, jurisdictions and booleans, not clause text), so the
body-level screen routes it to the shared pool. **No PII text leaves the
approved set** — the "violation" is definitional. V19 moves containment
from *document-level flags* to *request-body content*, and the two
definitions disagree exactly on the aggregation step whose prompt is
redacted by construction. The violation counter keeps its flag-based
meaning (comparability with every recorded run); the honest reading of a
single-entry run is: zero content leaks, one flag-level disagreement on
the report.

### 19.5 Posture

Config F stays a measured experiment, not the default policy: the
two-entry graph remains the RC1 record and the resilience posture
(failover steps, sensitive fail-closed). What V19 proves is that the
decision *can* live entirely in the gateway chain — client-unforgeable,
auditable per request (one `gci_screen` log line joins the policy
scanner's observe lines and the pipeline's screen) — at identical
quality, routing, and cost, with the report-step definitional shift on
the record. Adopting it as the default is a policy decision for a future
date, alongside the quay publish of `v0.9.3`.

Operational notes from the bring-up, for whoever repeats this: the praxis
Service needed its `:8086` screen port declared (§16.7.1); `oc apply -k`
resets both the deployment image and `PRAXIS_CONFIG` to the manifest
defaults, so experiment configs must be re-pinned after every apply and
verified from the pod's watcher log line before measuring; and the
v0.9.3 image must be built from a context whose `Cargo.lock` already
contains the vendored crates — an in-container re-lock dropped
`gci-screen-filter` from the build-time filter discovery, producing a
healthy-looking binary with no `gci_screen` registration (zero name
strings in the binary; `llm_d_sc` and the policy host registered fine).
`build.sh` should pre-update the lock at stage time before the next
publish.
