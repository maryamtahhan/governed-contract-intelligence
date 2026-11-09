# Stage runbook — one page

**Default is replay.** It is the measured ROSA run, not a live cluster. The
three beats below are the same in replay and live; only beat 2 needs a live
cluster (or the recording fallback).

Read the header run line before every beat: in the default dashboard **A is
the gateway-routed ROSA run** (`runs/rosa-200/hybrid`) and **B is the direct
GPU run** (`runs/direct/gpu-moe`). The appendix CUAD example reverses that
order.

## Pre-flight (5 minutes)

```bash
cd demo
uv run pytest -q                 # 206 passed
make dashboard-up                # 127.0.0.1:8099, replay only
open http://127.0.0.1:8099/
curl -s http://127.0.0.1:8099/api/health   # {"ok":true,...,"live_enabled":false}
```

Stop it with `make dashboard-down` when finished. Do not leave the dashboard
running overnight.

## Beat 1 — containment (replay A)

Leave the A/B toggle on **A**. On the ROSA run the header reads
`measured · <redacted-cluster> · rosa`; the hardware line names
`vllm-cpu` and `vllm-gpu-approved` and ends with **0 sensitive violations**.
Point at the routing figures: **247/247 sensitive steps on approved pools,
0 violations**, and **1200 CPU / 1 approved-GPU** general steps.

Say: *“This is the recorded routing evidence. The sensitive entry never
reached a non-approved pool; when the approved pool was unavailable it failed
closed instead of spilling.”*

Do **not** toggle to B for this beat. B is the direct run and has no gateway,
entry, pool or trace records.

## Beat 2 — pull the plug (live only, or the recording)

Replay cannot show a live outage. Use the cluster, or play the screen
recording listed under **Fallback** below.

During a live arm run, kill the GPU pool and watch the trace UI:

```bash
# terminal 1 — one small gateway run (simple + sensitive document)
DOC_IDS=C-2026-0001,C-2026-0003 ARMS=hybrid CONCURRENCY=2 \
  RUNS=runs/live-$(date +%Y%m%d) CLUSTER=<redacted-cluster> \
  scripts/cluster/arms.sh
# terminal 2 — while it runs
scripts/demo/pull-the-plug.sh                 # scales gpumoe-vllm to 0
# sensitive fail-closed: point a sensitive run at the approved pool, then
scripts/demo/pull-the-plug.sh --pool vllm-gpu-approved
scripts/demo/pull-the-plug.sh --restore       # scale it back to 1
```

Watch with `oc -n openshell-demo port-forward svc/trace-ui 8080:8080` and
open `http://127.0.0.1:8080`. The general entry degrades to CPU with
`x-praxis-fallback: true`; the sensitive entry returns an explicit 502/503 and
never spills to a non-approved pool.

**Fallback:** play the pre-recorded clip (record it once in prep; keep it at
`docs/media/pull-the-plug.mp4`). Say plainly: *“This is a recording of the
2026-11-09 ROSA run; the live cluster is not required for the talk.”* If the
clip is missing, skip the outage claim and show the recorded trace rows from
the last live run instead.

## Beat 3 — GPU economics (replay A)

Stay on **A**. Quote the measured run, not a model estimate:

- **0.036 GPU-seconds/document** vs **1.974** for all-GPU — **54× less**.
- **7.3 GPU-seconds total** vs **394.8** over 200 documents.
- At the illustrative **$1.10/GPU-hour**: about **$0.002** of GPU time for
  200 contracts vs **$0.12** all-GPU.

Say: *“The saving is the GPU capacity you did not have to buy or queue for.”*
Quality is supporting evidence, not the headline: extraction F1 **0.690**,
risk **0.632**, report **0.645** with the current scorer.

## Do not claim

- No governance or gateway routing from a **direct** run (default B, appendix
  A). It has no entry, label, pool or trace record.
- No **live** governance from a replay. The page is a recording; say “this is
  what the measured run recorded”, not “watch it govern now”.
- No extraction **fan-out** from a batch replay. The page replays completed
  documents; it does not prove concurrent streaming.
- No **pool name = hardware**. `vllm-gpu` is a tier name; read the hardware
  chip/provenance before claiming A10G.
- **Quality is not the headline.** Lead with containment and GPU economics.
- Do not quote **26.3%** for the report. That is the pre-fix value; the page
  and the re-scored `metrics.json` both read **64.5%**.
- Do not claim the **200-document run** was live today. It was measured on
  2026-11-04; today's live run is a separate, smaller run.

## Live cluster quick reference

The full bring-up is in `deploy/openshift/rosa/README.md`. The four stage
gotchas found on 2026-11-09:

1. The `trace-ui-server` ConfigMap needs **both** `server.py` and `otel.py`;
   with only `server.py` it crash-loops with `ModuleNotFoundError: otel`.
2. Delete the completed seeder **pods** (not the Jobs) before the pools start;
   they keep the RWO caches mounted:
   `oc -n openshell-demo delete pod -l 'job-name in (seed-model-cache-cpu,seed-model-cache-gpu)'`.
3. If a pool is stuck in `ContainerCreating` with
   `An operation with the given volume ... is already in progress`, restart the
   AWS EBS CSI node pods for that node; the mount then completes.
4. `scripts/demo/pull-the-plug.sh` now resolves the ROSA shared GPU
   Deployment (`gpumoe-vllm`); the parked `cpureal-vllm-gpu*` Deployments are
   not the live pool.
