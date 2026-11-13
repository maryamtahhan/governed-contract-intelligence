# Stage runbook — one page

**Default is replay.** It is the measured ROSA run, not a live cluster. The
three beats below are the same in replay and live; only beat 2 needs a live
cluster (or the recording fallback).

Read the header run line before every beat: in the default dashboard **A is
the current RC1 ROSA hybrid run**
(`runs/rosa-200-2026-11-12-praxis-routes/hybrid`) and **B is the same-run
one-pool control** (`runs/rosa-200-2026-11-12-praxis-routes/one-pool`). Both
cross Praxis; A is governed, B is not. B is the RC1 PII-gate boundary
control, not a quality comparison: the gate blocks sensitive work before the
pool. The 2026-11-11 A/B pair remains the last comparable four-arm matrix,
and the CUAD appendix instead uses a direct run for A and a failed gateway
run for B.

## Pre-flight (5 minutes)

```bash
cd demo
uv run --extra mcp pytest -q     # all green (MCP tests skip without the extra)
make dashboard-up                # 127.0.0.1:8099, replay only
open http://127.0.0.1:8099/
curl -s http://127.0.0.1:8099/api/health   # {"ok":true,...,"live_enabled":false}
```

Stop it with `make dashboard-down` when finished. Do not leave the dashboard
running overnight.

Beats 1 and 3 need no cluster and no port-forward: the replay above is the
whole surface. Only beat 2 (live failover) needs the cluster and
`oc -n openshell-demo port-forward svc/trace-ui 8080:8080` (or the recorded
clip fallback). The trace UI is live-only; it does not replay a run
directory.

## Beat 1 — containment (replay A)

Leave the A/B toggle on **A**. On the ROSA run the header reads
`measured · <redacted-cluster> · rosa`; the hardware line names
`vllm-cpu` and `vllm-gpu-approved` and ends with **0 sensitive violations**.
Point at the routing figures: **247/247 sensitive steps on approved pools,
0 violations**, **1000 CPU-served steps** (400 `vllm-cpu` for
classify/data-protection + 600 `cuad-specialist` for clause extraction) and
**201 judgment steps on GPU** (159 `vllm-gpu` + 42 approved). The current RC1
quality row is extract F1 **1.000**, risk **0.995**, report **0.998** and
**199/200** documents fully correct.

Say: *“This is the recorded routing evidence. The sensitive entry never
reached a non-approved pool; when the shared GPU backend was unavailable it
failed closed instead of spilling.”*

Do **not** toggle to B for this beat. B is the RC1 one-pool control: it
still crosses Praxis, but it has no sensitive listener, so the general-entry
PII gate denies 246 sensitive steps and one content miss reaches the pool
(1 violation). Its partial quality row is not comparable with A. Neither
view is a direct run.

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
scripts/demo/pull-the-plug.sh --restore       # scale it back to 1
```

Watch with `oc -n openshell-demo port-forward svc/trace-ui 8080:8080` and
open `http://127.0.0.1:8080`. On the real stack `vllm-gpu` and
`vllm-gpu-approved` select the same `gpumoe-vllm` Deployment, so do **not**
claim an approved-only outage. The recorded 2026-11-12 resilience run shows
the honest version: general classify falls back to the GPU with a visible
failed-hop trace, then recovers to `vllm-cpu`; with the shared GPU backend
down, sensitive risk and general risk both return 502, sensitive classify
stays healthy on `vllm-cpu`, and sensitive risk recovers after restart.
Evidence: `runs/rosa-resilience-2026-11-12/smoke.log`.

**Fallback:** play the pre-recorded clip (record it once in prep; keep it at
`docs/media/pull-the-plug.mp4`). Say plainly: *“This is a recording of the
2026-11-12 ROSA RC1 run; the live cluster is not required for the talk.”* If
the clip is missing, skip the outage claim and show the recorded trace rows
from the last live run instead.

## Beat 3 — GPU economics (replay A)

Stay on **A**. The current RC1 2026-11-12 run records routing, quality and
per-pool device time.

- Routing: **400 `vllm-cpu` + 600 `cuad-specialist`** CPU-served, **159
  `vllm-gpu` + 42 `vllm-gpu-approved`** judgment steps.
- Quality: extraction F1 **1.000**, risk **0.995**, report **0.998**,
  **199/200 documents fully correct**, **0 sensitive violations**.
- Cost: **0.259 GPU-s/doc** and **33.006 CPU core-s/doc** for the governed
  RC1 hybrid arm, with **201/201** judgment steps on GPU.
- Source: `runs/rosa-200-2026-11-12-praxis-routes/report.md`.
- The previous 2026-11-11 `v0.9.1` run was 806.59 s, F1/risk/report 1.000,
  200/200, **0.265 GPU-s/doc** and 32.555 CPU core-s/doc. That remains the
  last comparable four-arm matrix; the current RC1 controls are PII-gate
  boundary evidence, not cost comparisons.
- The **0.22 vs 1.013 GPU-s/doc** figure belongs to the pre-rebuild
  2026-11-09 graph (recoverable at `be3db42`) and is not a measurement of
  the rebuilt graph. Do not use the old **0.036 / 1.974 / 54×**
  classifier-in-path figures at all.

Say: *“The routing moved the expensive judgment work to GPU and kept the
high-volume clause work on CPU; the governance boundary held.”*

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
- Do not claim the **200-document run** was live today. It was recorded on
  2026-11-12 in replay; a live run is a separate, smaller run.
- Do not claim **Jev or the served screen** from this run. The recorded
  screen is the regex (41/0/159/0); `HYBRID_PII_DETECTOR=screen` and the
  `jev-cpu` control-plane route are implemented but not in the 2026-11-12
  record. The cause trigger is the regex; the opt-in Jev trigger failed its
  CUAD F1 guardrail (0.625 vs 0.714) and is not adopted.
- Do not claim an **approved-only outage** on the real stack. `vllm-gpu` and
  `vllm-gpu-approved` share the `gpumoe-vllm` Deployment. The measured RC1
  fail-closed result is for that shared backend, not an isolated approved
  pool.
- Do not quote the current RC1 controls as quality or cost comparisons. The
  general-entry PII gate blocks their sensitive work; their partial quality
  rows are not comparable with the hybrid. The 2026-11-11 four-arm matrix is
  the last comparable control set.
- RC1 key-to-step binding and the general-entry PII gate are **measured** on
  the 2026-11-12 real stack: 28/28 resilience checks, live gate assertions
  and the 200-document matrix. Say that, and cite
  `runs/rosa-resilience-2026-11-12` and
  `runs/rosa-policy-2026-11-12-real`.

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
