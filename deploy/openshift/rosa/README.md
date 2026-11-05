# ROSA (hosted control plane) CPU + GPU stack

Real CPU and GPU inference pools on a managed OpenShift cluster: the same
Qwen3.5-35B-A3B checkpoint served by a tainted x8i (Granite Rapids, AMX)
node and by 4x A10G on a g5 node, plus the 1.5B small pool and the CUAD
specialist composite. This is the environment behind the ROSA row of
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
   rosa create machinepool -c <cluster> --name mtahhan-g5-12xl \
     --instance-type g5.12xlarge --replicas 1 --subnet <private-subnet> \
     --labels demo.openshell.ai/node=gpu \
     --taints nvidia.com/gpu=present:NoSchedule

   # CPU pool: 48 physical cores, 2 NUMA nodes, AMX
   rosa create machinepool -c <cluster> --name mtahhan-x8i24xl \
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
   oc create ns openshell-demo
   oc -n openshell-demo create configmap trace-ui-server \
     --from-file=server.py=demo/src/hybrid_demo/traceui/server.py
   oc apply -f deploy/openshift/rosa/storage.yaml
   oc apply -f deploy/openshift/rosa/seeders.yaml
   oc -n openshell-demo wait --for=condition=complete \
     job/seed-model-cache-cpu job/seed-model-cache-gpu --timeout=3600s
   oc apply -k deploy/openshift/rosa
   ```

5. Build the specialist image (required for the specialist composite; the
   checkpoint is baked in at build time). On 4.22 use the dockerfilePath +
   `--from-dir` workaround - `oc start-build --dockerfile` is not supported:

   ```bash
   oc -n openshell-demo new-build --binary --strategy docker --name hybrid-specialist
   oc -n openshell-demo patch bc hybrid-specialist --type merge \
     -p '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"Dockerfile.specialist"}}}}'
   # stage only what the Dockerfile needs (--from-dir=demo uploads the
   # 950MB .venv and sits in git-clone: 40+ min measured 2026-11-04)
   STAGE=$(mktemp -d)
   cp demo/Dockerfile.specialist demo/pyproject.toml "$STAGE/"
   cp -R demo/src "$STAGE/src"
   oc -n openshell-demo start-build hybrid-specialist --from-dir="$STAGE" --follow
   ```

   The Dockerfile downloads the 1.4GB CUAD checkpoint *before* copying
   source, so a code-only rebuild (the `/metrics` fix) reuses that layer.
   `--from-archive` with a macOS bsdtar tarball is rejected by the builder
   ("must be a zip, tar, or gzipped tar"); use `--from-dir` on the small
   staging directory.

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

"Stacked" is arm D above - the composite pipeline over the same documents.
Score direct and stacked together (one `--runs`, space-separated) so the
report puts both rows in the same tables.

Sensitivity split: the report's `## Sensitive vs non-sensitive documents`
section uses the dataset's own `sensitive` label, never the pipeline screen
(that would be circular). It is only populated for synthetic data - CUAD
ships `sensitive: None`, and the report says so rather than folding those
documents into "non-sensitive".

## What was measured where

- 2026-11-04, eu-west-1 `gci-rosa`: x8i.24xlarge topology read and the
  bind lists in `pools-cpu.yaml` were generated from it (48 cores, 2 NUMA
  nodes x 24, siblings `(i, i+48)`, AMX bf16/int8). That cluster was torn
  down the same day when AWS showed no g5 capacity in the region; the
  overlay is unchanged and ports to another HCP cluster as long as the
  machine-pool labels/taints match.

- 2026-11-04, us-east-1 `models-arch-ocp` (ROSA HCP 4.22.6): the demo
  stack. Machine pools `mtahhan-g5-12xl` (g5.12xlarge, 4x A10G, TP=4) and
  `mtahhan-x8i24xl` (x8i.24xlarge, 48 cores) in us-east-1a; all four GPU
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
  `runs/rosa-200/report.md` and plan §6.2. Arm D's extraction was 600/800
  `cuad-specialist` responses (data protection stayed on the MoE), so the
  composite — not the MoE — did most of the extraction work.

  Direct baselines, same 200 documents, no router, concurrency 8:
  `cpu-moe` 2095.2 s, F1 1.0, risk 1.0, report 0.746, 200/200;
  `gpu-moe` 228.3 s, F1 1.0, risk 1.0, report 0.768, 200/200;
  `cpu-small` (1.5B) 594.3 s, F1 1.0, risk 0.395, report 0.212, 77/200.
  Zero errors in all three. Router overhead is negligible (direct GPU 228 s
  vs stacked arm A 202 s); all-CPU is 9.2x slower than all-GPU and 1.9x
  slower than D; the only quality loss in D is the specialist (F1 0.982),
  not the device; `cpu-small` fails exactly like arm B, so judgment quality
  follows the model. Direct device time was not sampled (`n/a`). Report:
  `runs/rosa-direct-vs-stacked/report.md`; plan §6.3; FINDINGS §15.7.

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
  `runs/rosa-200/classifier-replay-2026-11-04.txt`.

  Rebuilding the specialist: `oc start-build --from-dir=demo` uploads the
  950MB `.venv` and sat in git-clone for 40+ minutes. Build from a staging
  directory containing only `pyproject.toml`, `src/` and
  `Dockerfile.specialist`. The Dockerfile now downloads the 1.4GB
  checkpoint before copying source, so a code-only change reuses that
  layer. Note that `--from-archive` with a macOS bsdtar archive was
  rejected by the builder; `--from-dir` on the small staging directory is
  the reliable path.

## Teardown (2026-11-04)

Measurements were copied to `runs/` before anything was removed. Then:

- `oc delete namespace openshell-demo` removed every workload: deployments,
  services, routes, PVCs, builds, the specialist imagestream, configmaps,
  secrets and jobs. No PVs, EBS volumes, snapshots or load balancers remain
  from the stack.
- `rosa delete machinepool mtahhan-g5-12xl` and
  `rosa delete machinepool mtahhan-x8i24xl` removed both pools; the
  instances reached `terminated`, their ASGs are gone, and the node objects
  disappeared from the cluster.
- The empty `mtahhan-gci` namespace (created during the eu-west-1
  experiment) was also deleted.

The shared `models-arch-ocp` cluster and its other pools (`r6a-xlarge`,
`m6i-4xlg-enmaas`) were not changed, and the pre-existing
`nvidia-gpu-operator` namespace was left in place. The specialist image
remains only as unreferenced blobs in the internal registry until its
garbage collector prunes them.
