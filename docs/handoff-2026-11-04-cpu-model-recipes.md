# Handoff — Xeon 6 vLLM recipe review: which CPU models to run

**Date:** 2026-11-04
**Pick up:** 2026-11-05
**Status:** recommendation written; **no manifest or config changed**
**Source:** [recipes.vllm.ai/browse?hw=xeon6](https://recipes.vllm.ai/browse?hw=xeon6)
(catalog scanned via `models.json` + `/<hf_id>/hw/xeon6.json`, 2026-11-04)
**Scope:** `deploy/openshift/cpu-real*/`, `deploy/openshift/rosa/pools-cpu.yaml`,
`manifests/vllm-cpu-*.yaml`, `FINDINGS.md` §10–§11

---

## 1. The question

The project already runs the CPU tier on Xeon 6 (ROSA `x8i.24xlarge`, 48 cores,
AMX; 16 whole cores guaranteed per pool). The question was: given the official
vLLM recipe catalog, which models should the CPU pools actually serve?

## 2. What the catalog contains

- **221 recipes total; 40 render for `hw=xeon6`.**
- Every Xeon 6 rendering uses the same image and pattern:
  `vllm/vllm-openai-cpu:latest-x86_64`, `--tensor-parallel-size 1` by default,
  CPU pre-built wheels, `--privileged --ipc=host -v ~/.cache/huggingface`.
- Qwen3.5-family recipes additionally set `VLLM_USE_RUST_FRONTEND=1` and
  `VLLM_ENGINE_READY_TIMEOUT_S=1800` (cold load + CPU compilation exceeds the
  600 s default).
- The `xeon6` taxonomy profile is a **multi-socket** node (guides say
  "4× Xeon 6 CPUs"). Our ROSA pod is 16 cores inside one NUMA node, so these
  recipes are validated *configurations*, not capacity claims. TP stays 1 and
  `VLLM_CPU_OMP_THREADS_BIND=auto` as we already do.

The 40 in-catalog models, grouped:

| family | models validated on Xeon 6 |
|---|---|
| Qwen3.5 | `Qwen3.5-35B-A3B` (BF16 only), `Qwen3.5-4B` |
| Qwen3 | `Qwen3-30B-A3B`, `Qwen3-14B` (+AWQ, FP8), `Qwen3-8B`, `Qwen3-4B`, `Qwen3-1.7B`, `QwQ-32B` (+AWQ, W8A8) |
| Qwen VL | `Qwen2.5-VL-7B-Instruct`, `Qwen3-VL-30B-A3B-Instruct` |
| Llama | `Llama-3.2-1B`, `-1B-Instruct`, `-3B-Instruct` (+AWQ/W8A8), `Llama-3.1-8B(-Instruct)` (+AWQ-INT4/FP8/W8A8, TP=2), `Llama-3.3-70B-Instruct` (+FP8-block, TP=8), `Llama-4-Scout-17B-16E` |
| Google | `gemma-4-26B-A4B`, `gemma-4-E2B`, `gemma-4-E4B`, `diffusiongemma-26B-A4B` |
| IBM / Microsoft / OpenAI / Z.ai | `granite-3.2-2b-instruct`, `Phi-4-reasoning`, `Phi-4-multimodal`, `gpt-oss-20b` (MXFP4), `glm-4-9b-hf` |
| other | `whisper-large-v3` |

## 3. Recommendation

### 3.1 Keep `Qwen/Qwen3.5-35B-A3B` for synthesis on CPU — it is validated

This is the single most useful result: the model we already deploy is **the
exact xeon6 recipe**. Rendered command
([page](https://recipes.vllm.ai/Qwen/Qwen3.5-35B-A3B?hardware=xeon6)):

```bash
export VLLM_ENGINE_READY_TIMEOUT_S=1800
export VLLM_USE_RUST_FRONTEND=1
vllm serve Qwen/Qwen3.5-35B-A3B \
  --trust-remote-code \
  --tensor-parallel-size 1 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 \
  --mm-encoder-tp-mode data
```

`deploy/openshift/cpu-real-moe/kustomization.yaml` already passes
`--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3
--mm-encoder-tp-mode data --trust-remote-code --tensor-parallel-size 1` plus
`VLLM_ENGINE_READY_TIMEOUT_S=3600`. **No change needed.**

Hard limit to remember: on `xeon6` only the **BF16 `default` variant** is
validated. `variants=fp8` and `variants=gptq_int4` exist but render for
`arc_pro_b60`/XPU, not CPU — the site explicitly answers
"requested `hardware=xeon6` is not available for variant `gptq_int4`".
So there is **no CPU-quantized path** for this model, and the 67 GiB weights /
128 GiB limit sizing stands.

### 3.2 Benchmark `openai/gpt-oss-20b` as the CPU-synthesis challenger

The strongest alternative in the catalog for our constraint:

- 21B total / **3.6B active** MoE, **native MXFP4**, ~16 GB of weights.
- Explicitly Xeon 6-validated (`vLLM >= 0.10.1`), `--tool-call-parser openai`.
- Fits a 16-core / 64 GiB pool with real KV-cache headroom — unlike the 35B-A3B,
  which needs 128 GiB.
- Directly de-risks the concern already written into
  `cpu-real-moe/kustomization.yaml`: "vLLM's CPU MoE kernels are the least
  battle-tested path."

```bash
vllm serve openai/gpt-oss-20b \
  --max-num-batched-tokens 16384 \
  --gpu-memory-utilization 0.8 \
  --tool-call-parser openai \
  --enable-auto-tool-choice
```

Cost: harmony chat format, reasoning-on by default. It needs an A/B through the
CUAD scorer (same seam as the existing MoE comparison, `runs/cuad-mixed/arm-D-*`).

### 3.3 Keep `Qwen/Qwen3-30B-A3B` as the conservative MoE swap

BF16 Xeon 6-validated on `vLLM >= 0.8.5`, 30.5B/3.3B active, same 30B-class
footprint, **no gated-delta/Mamba path**. If 35B-A3B misbehaves on CPU — its
recipe notes Mamba prefix caching is experimental and long context is a separate
feature gate — this is the least-risk drop-in that keeps 30B-class knowledge.

### 3.4 Dense fallback: `Qwen3.5-9B` has no Xeon 6 recipe

`Qwen/Qwen3.5-9B` returns **404** for `hw/xeon6`, so
`deploy/openshift/cpu-real-9b/` is running an unvalidated combination. The
nearest validated dense models are `Qwen/Qwen3-14B` (BF16, AWQ, FP8 all render
for xeon6) and `Qwen/Qwen3-8B`. If the dense fallback is ever used for a quoted
comparison, switch to `Qwen3-14B` or say plainly that the 9B is off-catalog.
Be honest about the trade: 14B dense on 16 cores will lose to a 3B-active MoE on
tokens/s, so this is a debugging fallback, not an upgrade.

### 3.5 Small / classify tier: swap `Qwen2.5-1.5B-Instruct` → `Qwen3-1.7B`

`Qwen/Qwen2.5-1.5B-Instruct` also has no xeon6 recipe. `Qwen/Qwen3-1.7B` does:

```bash
vllm serve Qwen/Qwen3-1.7B \
  --tensor-parallel-size 1 \
  --max-num-batched-tokens 16384 \
  --gpu-memory-utilization 0.8 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --reasoning-parser qwen3
```

Same role (SIMPLE/classify), same served name. `granite-3.2-2b-instruct` and
`gemma-4-E2B-it` are also in-catalog if a labels-only classifier is preferred.

### 3.6 Not covered by vLLM recipes — leave alone

- **CUAD specialist** (`roberta-large` QA, `cuad-specialist`): vLLM has no
  extractive-QA task. Keep the custom server
  (`demo/src/hybrid_demo/specialist/server.py`).
- **Embedders** (litil-embed-0.6b, octen-law-8b, dinghy-law-0.6b): no vLLM
  recipe; `jina-embeddings-v5-text-small` exists but has **no xeon6 rendering**.
  Keep the current serving path. FINDINGS §11 stands: the 0.6B wins within-doc.
- **docling**: ingestion front-end, not a model server.

## 4. What this does *not* change

The recipes say what will **run** on Xeon 6, not what will be interactive. Our
own measurement already answers the latency question: on CPU, classify took
3.4–21.3 s vs ~0.6–1 s on 4× A10G (FINDINGS §10, cross-hardware parity). CPU
remains the isolation / governance / batch-posture tier. A faster CPU model
narrows the gap; it does not remove it.

## 5. Tomorrow's plan (in order)

1. **Catalog drill-down (30 min, no cluster).** Render the two challengers and
   diff their flags against our manifests:
   ```bash
   curl -s "https://recipes.vllm.ai/openai/gpt-oss-20b.json?hardware=xeon6" | jq -r .command
   curl -s "https://recipes.vllm.ai/Qwen/Qwen3-30B-A3B.json?hardware=xeon6" | jq -r .command
   curl -s "https://recipes.vllm.ai/Qwen/Qwen3-1.7B.json?hardware=xeon6" | jq -r .command
   ```
2. **Stand up `gpt-oss-20b` in a spare 16-core slot** (reuse the parked NUMA-1
   slot in `rosa/pools-cpu.yaml`) with the same `--max-model-len 16384`,
   `--max-num-seqs 32`, guaranteed QoS and OMP binding. Measure cold-start,
   then `vllm bench serve` on the two workloads in FINDINGS §9 (extract-only,
   full mix) at c=1/4/16.
3. **Quality A/B**: run the CUAD direct arm against `gpt-oss-20b` on the same
   19 held-out docs and compare F1 / risk / report to the 35B-A3B CPU numbers
   (F1 0.690, risk 0.632, report 0.658). Nothing gets promoted on latency alone.
4. **Only if 35B-A3B CPU fails**, test `Qwen3-30B-A3B` as the fallback before
   touching the dense 9B.
5. Whatever wins, record it in FINDINGS with the same arms/run layout — the
   decision rule for this repo is measured, not catalog-based.

Suggested ranking to test, best guess first:
`Qwen3.5-35B-A3B` (incumbent, validated) → `gpt-oss-20b` (smaller, faster,
MXFP4) → `Qwen3-30B-A3B` (conservative swap).

## 6. Warnings

- **Do not cite the 40 recipes as "supported on our CPU pool."** The profile is
  a multi-socket Xeon 6 node; our pools are 16-core NUMA-local slices. The
  recipe is the validated flag set, not a throughput promise.
- **No quantized CPU 35B-A3B.** Don't try `--quantization` with the GPTQ/FP8
  checkpoints on the CPU image because the catalog lists them — the xeon6
  rendering does not exist for those variants.
- **Changing `--served-model-name` breaks the relays.** All CPU pools serve as
  `Semantic-Qwen`; keep that name if swapping the model so praxis routes and
  the trace UI keep working.
- **Cold start is the real rollout risk, not steady state.** Empty `emptyDir`
  model cache plus CPU compilation is what blew past readiness probes before
  (§7 of FINDINGS). Keep `VLLM_ENGINE_READY_TIMEOUT_S >= 1800`, seed caches,
  and raise probe `initialDelaySeconds` to match.

## 7. Files to read first tomorrow

- `deploy/openshift/cpu-real-moe/kustomization.yaml` — current 35B-A3B args
- `deploy/openshift/cpu-real-9b/kustomization.yaml` — off-catalog dense fallback
- `deploy/openshift/rosa/pools-cpu.yaml` — pool sizing, parked NUMA-1 slot
- `deploy/openshift/cpu-real-specialist/specialist.yaml` — RoBERTa server
- `FINDINGS.md` §9 (bench harness), §10 (CPU/GPU parity), §11 (retrieval tiers)
- `runs/cuad-mixed/` — the existing model-variant comparison layout
