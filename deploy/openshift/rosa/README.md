# ROSA (hosted control plane) CPU + GPU stack

Real CPU and GPU inference pools on a managed OpenShift cluster: the same
Qwen3.5-35B-A3B checkpoint served by a tainted x8i (Granite Rapids, AMX)
node and by 4x A10G on a g5 node, plus the 1.5B small pool and the CUAD
specialist model endpoint. Praxis maps each work step to one of these routes. This is the environment behind the ROSA row of
FINDINGS §1.

Read `kustomization.yaml` first - its header explains every deliberate
divergence from the SNO overlays (no KServe objects, OMP core pinning
instead of a static CPU manager, CPU request without limit, per-node gp3
model caches, digest-pinned vLLM image).

## Prerequisites

- Cluster editor on the target cluster in OCM (machine pools are managed
  through OCM; cluster-admin alone cannot add nodes).
- `AWS_PROFILE`/`AWS_REGION` set for the account that owns the cluster.
- For cluster-admin inside the cluster:
  `rosa create admin -c <cluster>` then `oc login <api> --username cluster-admin`.

## Bring-up order

1. Machine pools. Both pools must land in ONE AZ (the measured request path
   and the gp3 volumes are AZ-scoped). The labels and taints below are the
   contract the overlay expects - do not rename them.

   ```bash
   # GPU pool: 4x A10G
   rosa create machinepool -c <cluster> --name <redacted-gpu-pool> \
     --instance-type g5.12xlarge --replicas 1 --subnet <private-subnet> \
     --labels demo.openshell.ai/node=gpu \
     --taints nvidia.com/gpu=present:NoSchedule

   # CPU pool: 48 physical cores, 2 NUMA nodes, AMX
   rosa create machinepool -c <cluster> --name <redacted-cpu-pool> \
     --instance-type x8i.24xlarge --replicas 1 --subnet <private-subnet> \
     --labels demo.openshell.ai/node=cpu-infer \
     --taints demo.openshell.ai/cpu-infer=true:NoSchedule
   ```

   Pool names are capped at 15 characters by OCM.

   Note: g5 capacity is region/AZ-dependent and can be exhausted for hours
   (eu-west-1 had none in any AZ on 2026-11-04; us-east-1a did). Check with
   a throwaway `aws ec2 run-instances` probe in the target subnet before
   committing to a cluster.

2. GPU operator (start early - the driver install takes 10-15 min):

   ```bash
   oc apply -f deploy/openshift/rosa/gpu-operator.yaml
   oc -n nvidia-gpu-operator wait --for=condition=ready \
     clusterpolicy/gpu-cluster-policy --timeout=1200s
   ```

   The OperatorGroup must target its own namespace (SingleNamespace); the
   operator's CSV does not support AllNamespaces and OLM fails the install
   with UnsupportedOperatorGroup otherwise. If a cluster already runs the
   operator, this file is idempotent.

3. Regenerate the core bind lists from the real node:

   ```bash
   scripts/cluster/cpu-topology.sh <x8i-node-name>
   ```

   Paste the proposed `VLLM_CPU_OMP_THREADS_BIND` values into
   `pools-cpu.yaml`. Never record latency numbers from a run whose lists
   were not generated from the node it ran on.

4. Seed the caches, then deploy:

   ```bash
   oc create ns <redacted-namespace>
   # server.py imports otel.py beside it; a ConfigMap with only server.py
   # crash-loops with ModuleNotFoundError: No module named 'otel'.
   oc -n <redacted-namespace> create configmap trace-ui-server \
     --from-file=server.py=demo/src/hybrid_demo/traceui/server.py \
     --from-file=otel.py=demo/src/hybrid_demo/traceui/otel.py \
     --dry-run=client -o yaml | oc apply -f -
   oc apply -n <redacted-namespace> -f deploy/openshift/rosa/storage.yaml
   oc apply -n <redacted-namespace> -f deploy/openshift/rosa/seeders.yaml
   oc -n <redacted-namespace> wait --for=condition=complete \
     job/seed-model-cache-cpu job/seed-model-cache-gpu --timeout=3600s
   # Completed seeder pods keep the RWO caches mounted; delete just the pods
   # (not the Jobs, which apply -k would recreate) before the pools start.
   oc -n <redacted-namespace> delete pod \
     -l 'job-name in (seed-model-cache-cpu,seed-model-cache-gpu)'
   oc apply -k deploy/openshift/rosa
   ```

5. Build the specialist image (required for the `cuad-specialist` route; the
   checkpoint is baked in at build time). On 4.22 use the dockerfilePath +
   `--from-dir` workaround - `oc start-build --dockerfile` is not supported:

   ```bash
   oc -n <redacted-namespace> new-build --binary --strategy docker --name hybrid-specialist
   oc -n <redacted-namespace> patch bc hybrid-specialist --type merge \
     -p '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"Dockerfile.specialist"}}}}'
   # stage only what the Dockerfile needs (--from-dir=demo uploads the
   # 950MB .venv and sits in git-clone: 40+ min measured 2026-11-04)
   STAGE=$(mktemp -d); mkdir -p "$STAGE/src"
   cp demo/src/docker/Dockerfile.specialist demo/pyproject.toml "$STAGE/"
   cp -R demo/src/hybrid_demo "$STAGE/src/hybrid_demo"
   oc -n <redacted-namespace> start-build hybrid-specialist --from-dir="$STAGE" --follow
   ```

   The Dockerfile downloads the 1.4GB CUAD checkpoint *before* copying
   source, so a code-only rebuild (the `/metrics` fix) reuses that layer.
   `--from-archive` with a macOS bsdtar tarball is rejected by the builder
   ("must be a zip, tar, or gzipped tar"); use `--from-dir` on the small
   staging directory.

6. Build the Jev image **only if you are enabling the opt-in `screen:pii`
   route**. `jev-cpu` is in the kustomization, but the 2026-11-12 RC1 run
   used the regex screen and did not need it. The English checkpoint is baked
   into the image at build time:

   ```bash
   oc -n <redacted-namespace> new-build --binary --strategy docker --name hybrid-jev
   oc -n <redacted-namespace> patch bc hybrid-jev --type merge \
     -p '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"Dockerfile.jev"}}}}'
   STAGE=$(mktemp -d); mkdir -p "$STAGE/src"
   cp demo/src/docker/Dockerfile.jev demo/pyproject.toml "$STAGE/"
   cp -R demo/src/hybrid_demo "$STAGE/src/hybrid_demo"
   oc -n <redacted-namespace> start-build hybrid-jev --from-dir="$STAGE" --follow
   ```

   The service starts with `--warmup`, so the readiness probe only passes
   after the baked checkpoint loads. Point the runner at the screen ingress,
   not at `jev-cpu` directly:
   `HYBRID_PII_DETECTOR=screen HYBRID_PII_SCREEN_URL=http://<trace-ui>:8086/v1/systemone`.
   The trace UI records these calls with `entry=screen`; they are not review
   steps.

## Recording runs

Pass the ROSA provenance so records cannot be confused with SNO runs:

```bash
--environment rosa --cluster <cluster-name> \
  --pools @deploy/openshift/rosa/pools.json
```

The demo matrix, in the order it is usually run:

```bash
# 1. live demo: one non-sensitive + one sensitive document through the
#    governed arm. --docs 1 alone picks a NON-sensitive document (golden
#    ordering), so select explicitly.
DOC_IDS=C-2026-0001,C-2026-0003 ARMS=D DOCS=2 \
  scripts/cluster/arms.sh

# 2. batch: 200 synthetic documents (41 sensitive), all four routing arms
DOCS=200 ARMS="A B C D" scripts/cluster/arms.sh

# 3. direct: the single-model baseline per pool, quality only (no routing).
#    Bypasses praxis on purpose; port-forwards, not Routes. The pools are
#    independent, so run them in parallel on separate local ports (they write
#    to runs/direct/<pool>/; regenerate one combined report at the end).
DOCS=200 CONCURRENCY=8 POOLS="cpu-moe=cpureal-vllm-cpu" LOCAL_BASE=19100 \
  scripts/cluster/direct.sh
DOCS=200 CONCURRENCY=8 POOLS="gpu-moe=gpumoe-vllm" LOCAL_BASE=19200 \
  scripts/cluster/direct.sh
DOCS=200 CONCURRENCY=8 POOLS="cpu-small=cpureal-vllm-gpu-small" LOCAL_BASE=19300 \
  scripts/cluster/direct.sh
```

The direct report input is one compact line per document, not one line per
extraction record: at 200 documents the record-per-line form is ~229k chars
(~57k tokens) against the 16k `max-model-len` all pools serve, so the report
request fails with HTTP 400 *after* every per-document step has been paid
for. `report_lines()` in `pipeline/direct.py` and its regression test encode
the fix.

"Stacked" is arm D above - the staged pipeline over the same documents.
Score direct and stacked together (one `--runs`, space-separated) so the
report puts both rows in the same tables.

Sensitivity split: the report's `## Sensitive vs non-sensitive documents`
section uses the dataset's own `sensitive` label, never the pipeline screen
(that would be circular). It is only populated for synthetic data - CUAD
ships `sensitive: None`, and the report says so rather than folding those
documents into "non-sensitive".

## What was measured where

- 2026-11-04, eu-west-1 `<redacted-cluster-eu>`: x8i.24xlarge topology read and the
  bind lists in `pools-cpu.yaml` were generated from it (48 cores, 2 NUMA
  nodes x 24, siblings `(i, i+48)`, AMX bf16/int8). That cluster was torn
  down the same day when AWS showed no g5 capacity in the region; the
  overlay is unchanged and ports to another HCP cluster as long as the
  machine-pool labels/taints match.

- 2026-11-04, us-east-1 `<redacted-cluster>` (ROSA HCP 4.22.6): the demo
  stack. Machine pools `<redacted-gpu-pool>` (g5.12xlarge, 4x A10G, TP=4) and
  `<redacted-cpu-pool>` (x8i.24xlarge, 48 cores) in us-east-1a; all four GPU
  devices allocatable; the x8i topology reproduced the recorded bind lists
  exactly (`scripts/cluster/cpu-topology.sh`, `problems: []`).

  Two bring-up fixes are baked into the overlay after this run:

  1. **KV cache pinned + 192 GiB limit** on the MoE pool. At 128 GiB,
     reading the 67 GiB checkpoint filled the cgroup page cache and
     vLLM's auto KV calculation (80% of the limit) failed: "0.58 GiB
     available vs 30.6 GiB requested". `VLLM_CPU_KVCACHE_SPACE=16` makes
     the allocation explicit; the 192 GiB limit leaves room for the page
     cache. 16 GiB is ample for 16k context x 32 sequences.
  2. **`taskset -c <list> vllm serve`** as the container command. The
     `VLLM_CPU_OMP_THREADS_BIND` + libiomp5 path logs the right core list
     but applies nothing in this image (the engine forks after the
     preloaded runtime initialises): measured 234/234 engine threads on
     0-95 before the switch, 126 threads on 0-15 after (1.5B on 24-35).

  Smoke run with 1 non-sensitive + 1 sensitive synthetic document, arm D:
  0 violations, 7/7 sensitive steps on approved pools, extract F1 1.0,
  risk 1.0, 2/2 documents fully correct, 26.8 s wall.

  Batch, 200 synthetic documents (41 sensitive), arms A-D: 0 errors.
  Headline: D 1111.8 s, extract F1 0.982, risk 0.995, 173/200 fully
  correct, 99.9% CPU-served, 247/247 sensitive steps on approved pools,
  0 violations, 0.036 GPU-s/doc; A (all-GPU) 202.2 s and 1.974 GPU-s/doc,
  247 violations; C (round-robin) 612.9 s, 122 violations. Full tables:
  `runs/rosa-200/report.md` (archived at `ea80dfb`) and plan §6.2. Arm D's extraction was 600/800
  `cuad-specialist`-labelled responses (data protection stayed on the MoE),
  so the specialist route — then still a composite pod — did most of the
  extraction work. The 2026-11-10 rebuild separates the two routes.

- 2026-11-10, us-east-1 `<redacted-cluster>` (rebuilt two-entry graph). Machine
  pools re-created with the same labels/taints (`<redacted-gpu-pool>`,
  `<redacted-cpu-pool>` in us-east-1a); the CPU topology script reproduced the
  recorded bind lists exactly (`problems: []`). Batch, 200 synthetic
  documents (41 sensitive), arms A-D, concurrency 12, recorded in
  `runs/rosa-200-2026-11-10-praxis-routes/`. Hybrid: 839.9 s, 0 errors,
  0 fallback, extract F1 1.000, risk 1.000, report 0.750, 200/200 fully
  correct, 0 sensitive violations. Route counts: `vllm-cpu` 400,
  `cuad-specialist` 600, `vllm-gpu` 159, `vllm-gpu-approved` 42; 247/247
  sensitive steps on approved pools. The all-GPU control hit 11 transport
  timeouts on termination prompts (601.2 s, 245 violations); gpu-small
  457.6 s / 247 violations; round-robin 938.4 s / 117 violations. Device
  time for the rebuilt hybrid arm is `n/a` (relay gauges unavailable), not
  zero; do not reuse the pre-rebuild 0.22 GPU-s/doc figure for this graph.

  Direct baselines, same 200 documents, no router, concurrency 8:
  `cpu-moe` 2095.2 s, F1 1.0, risk 1.0, report 0.746, 200/200;
  `gpu-moe` 228.3 s, F1 1.0, risk 1.0, report 0.768, 200/200;
  `cpu-small` (1.5B) 594.3 s, F1 1.0, risk 0.395, report 0.212, 77/200.
  Zero errors in all three. Router overhead is negligible (direct GPU 228 s
  vs stacked arm A 202 s); all-CPU is 9.2x slower than all-GPU and 1.9x
  slower than D; the only quality loss in D is the specialist (F1 0.982),
  not the device; `cpu-small` fails exactly like arm B, so judgment quality
  follows the model. Direct device time was not sampled (`n/a`). Report:
  `runs/rosa-direct-vs-stacked/report.md` (archived at `ea80dfb`); plan §6.3;
  FINDINGS §15.7.

  Three instrumentation/scoring fixes came out of the run:

  1. **Specialist `/metrics` was JSON-quoted.** FastAPI's default
     `JSONResponse` rendered the Prometheus text as a JSON string, so it
     was unscrapable; now `PlainTextResponse`. The sampler also only
     matched dotted names and missed underscore-only gauges such as
     `vllm_num_requests_running`. Verified live on image
     `hybrid-specialist-5` (digest `sha256:f44ef061…`): `/metrics` starts
     with `# HELP specialist_requests_total` and the gauge parses.
  2. **Cost scoring separates measured from unmeasured device time.** A
     pool with no parseable gauge prints `n/a`, never `$0.0`, and an
     unmeasured arm gets no dollar figure. GPU time is measured and
     decisive; CPU core-seconds still are not.
  3. **Direct-arm report input is per-document.** Scoring the first
     200-document direct run also exposed that direct runs write quality
     scores to `metrics.json`; the scorer now ignores that file when it
     is not a pool->summary map instead of crashing.

  On the classifier: 1,201 steps labelled, 0.1% agreement with the
  generator's per-step labels. Replaying the real prompts through the
  image's own `llm-d-sc-classify` shows why: classify/extract/risk all
  sit at MEDIUM ~0.99; the 200-document report sits at REASONING 0.707
  vs MEDIUM 0.398. The boundary is single-document -> CPU vs
  cross-document -> GPU. No exposed setting changes that (the server
  exposes only the taxonomy name; praxis `min_score` cannot separate
  0.3-0.99 margins), and it is the boundary the policy needs. Evidence:
  `runs/rosa-200/classifier-replay-2026-11-04.txt` (archived at `ea80dfb`).

  Rebuilding the specialist: `oc start-build --from-dir=demo` uploads the
  950MB `.venv` and sat in git-clone for 40+ minutes. Build from a staging
  directory containing only `pyproject.toml`, `src/` and
  `Dockerfile.specialist`. The Dockerfile now downloads the 1.4GB
  checkpoint before copying source, so a code-only change reuses that
  layer. Note that `--from-archive` with a macOS bsdtar archive was
  rejected by the builder; `--from-dir` on the small staging directory is
  the reliable path.

- 2026-11-11, us-east-1 `<redacted-cluster>` (rebuilt two-entry graph, cleaned
  stack). The same topology and pools, re-run after the OpenShell sandbox
  egress fix and the deterministic report rollup. Previous comparable record
  (`v0.9.1`): `runs/rosa-200-2026-11-11-praxis-routes/`. Hybrid: **806.59 s,
  0 errors, 0 fallback, 247/247 sensitive steps on approved pools, 0
  violations, extract F1 1.000, risk 1.000, report 1.000, 200/200 fully
  correct**, with **0.265 GPU-s/doc** and **32.555 CPU core-s/doc**. Route
  counts unchanged: `vllm-cpu` 400, `cuad-specialist` 600, `vllm-gpu` 159,
  `vllm-gpu-approved` 42. Same-run controls: one-pool 201.89 s / 0.981
  GPU-s/doc / 247 violations; small-first 489.92 s / 2.429 GPU-s/doc /
  risk 0.395 / 247 violations; round-robin 935.56 s / 1.862 GPU-s/doc / 110
  violations. Direct baselines: `cpu-moe` 2949.5 s, `gpu-moe` 301.6 s,
  `cpu-small` 1144.4 s (risk 0.395, report 0.212). The 2026-11-10 record is
  preserved as the earlier matrix. RC1 is not in this run.
- 2026-11-12, us-east-1 `<redacted-cluster>` (RC1 `v0.9.2`, real pools).
  Pools were recreated in `us-east-1a`, cache seeds completed, and the CPU
  topology reproduced `0-15` / `16-17` / `24-35` with `problems: []`.
  **Current quoted record:** `runs/rosa-200-2026-11-12-praxis-routes/`.
  Hybrid: **818.87 s, 0 errors, 0 fallbacks, 247/247 sensitive steps
  approved, 0 violations, extract F1 1.000, risk 0.995, report 0.998,
  199/200 fully correct**, with **0.259 GPU-s/doc** and **33.006 CPU
  core-s/doc**. The same stack passed the 2-document smoke
  (`runs/rosa-smoke-2026-11-12`), live gate assertions
  (`runs/rosa-policy-2026-11-12-real`) and the **28/28** `STACK=rosa`
  resilience smoke (`runs/rosa-resilience-2026-11-12`): CPU
  fallback/recovery and shared-GPU sensitive fail-closed/recovery. The
  current controls (`one-pool` 157.03 s, `small-first` 377.68 s,
  `round-robin` 725.35 s) are PII-gate boundary evidence: 246 denials and 1
  sensitive violation each, not quality or cost comparisons. On this stack
  `vllm-gpu` and `vllm-gpu-approved` select the same `gpumoe-vllm`
  Deployment, so an approved-only outage cannot be isolated.

## Teardown (2026-11-04, repeated 2026-11-11 and 2026-11-12)

The 2026-11-12 RC1 run used the same bring-up and teardown shape: demo
deployments, jobs, pods and PVCs were deleted after the matrix was recorded,
the three cache PVs and EBS volumes were removed, and the
`<redacted-gpu-pool>` / `<redacted-cpu-pool>` machine pools were deleted the same
day. The nodes and instances terminated. **The cluster stack is down.**
Rebuild from this README before any new cluster measurement; the 2026-11-12
runs are in git and are the current quoted record. Teardown evidence:
`runs/rosa-resilience-2026-11-12/teardown.txt`.

Measurements were copied to `runs/` before anything was removed. Then:

- The demo Deployments, Jobs, Pods and PVCs were deleted; the Services,
  Routes, ConfigMaps, BuildConfigs, secrets and namespace were left in
  place. The three model-cache PVs and their EBS volumes were deleted with
  the PVCs; `runs/rosa-resilience-2026-11-12/teardown.txt` records that all
  three volume IDs returned `InvalidVolume.NotFound`.
- `rosa delete machinepool <redacted-gpu-pool>` and
  `rosa delete machinepool <redacted-cpu-pool>` removed both pools; the
  instances reached `terminated`, their ASGs are gone, and the node objects
  disappeared from the cluster.
- Three unrelated 200 GiB `<redacted-volume-tag-1>` retain volumes in other
  namespaces were present before teardown and were left untouched.
- The empty `<redacted-eu-namespace>` namespace (created during the eu-west-1
  experiment) was also deleted.

The shared `<redacted-cluster>` cluster and its other pools (`r6a-xlarge`,
`m6i-4xlg-enmaas`) were not changed, and the pre-existing
`nvidia-gpu-operator` namespace was left in place. The specialist image
remains only as unreferenced blobs in the internal registry until its
garbage collector prunes them.
