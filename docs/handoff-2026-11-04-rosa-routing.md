# Handoff — ROSA 200-document run and the "all-CPU" routing question

**Date:** 2026-11-04
**Pick up:** 2026-11-05
**Status:** analysis complete; decision taken 2026-11-05 (A + B); B implemented,
not yet measured on ROSA
**Scope:** `runs/rosa-200/`, `docs/stage-review-walkthrough.html` (§ "ROSA 200-document run"),
`deploy/openshift/rosa/`, `manifests/base/praxis/`, `demo/src/hybrid_demo/`

---

## 1. The issue in one paragraph

The ROSA 200-document matrix (config D, `runs/rosa-200/hybrid/`) is the only arm
with **0 sensitive violations**, but it ran as **all-CPU behind a governance
gate**, not as complexity routing: llm-d-sc labelled **1,200 of 1,201 steps
MEDIUM** and one REASONING. D sent **1,200 steps to `vllm-cpu`** and one to
`vllm-gpu-approved`. Praxis was healthy — **0 errors, 0 fallbacks, 1,201/1,201
served**. The guide's "Do not overclaim the routing" callout is accurate.

## 2. Why it happened (the concrete lever)

It is not only the classifier. The ROSA overlay points the specialist composite
at the **CPU MoE**:

`deploy/openshift/rosa/specialist.yaml` (line ~46):

```yaml
"--forward-to", "http://cpureal-vllm-cpu:8000"
```

The specialist answers extraction prompts with the CUAD RoBERTa checkpoint and
forwards every other prompt to that URL. On this run that meant:

| stage | calls | answered by | route recorded |
|---|---:|---|---|
| classify | 200 | `Semantic-Qwen` (CPU MoE) via specialist forward | `vllm-cpu` |
| extract (3 clauses) | 600 | `cuad-specialist` (CPU) | `vllm-cpu` |
| extract (data protection) | 200 | `Semantic-Qwen` (CPU MoE) via specialist forward | `vllm-cpu` |
| risk | 200 | `Semantic-Qwen` (CPU MoE) via specialist forward | `vllm-cpu` |
| report | 1 | `Semantic-Qwen` on GPU (praxis REASONING) | `vllm-gpu-approved` |

So **600 generative calls never reached the GPU** even though the GPU MoE
(`gpumoe-vllm` / `gpumoe-vllm-approved`) was up and serving the same weights.
Only the report used the GPU.

Supporting evidence:

- `runs/rosa-200/hybrid/results.json` → `summary.routes`, `summary.labels`,
  `summary.stage_models`
- `runs/rosa-200/hybrid/summary.json` → same summary, wall time, provenance
- `runs/rosa-200/classifier-replay-2026-11-04.txt` → single-document prompts
  score ~0.99 MEDIUM; the portfolio report scores 0.707 REASONING. No exposed
  llm-d-sc knob changes this (no classification threshold; `min_score` cannot
  separate margins of 0.3–0.99).
- `FINDINGS.md` §4, §15.5 — same conclusion, already written down.

## 3. The key trade-off (do not miss this)

**The 54× GPU saving is the same fact as the 99.9% CPU share.** If you move the
600 forwarded calls to GPU, you fix "all-CPU" but give back most of the cost
headline. Rough estimate from arm A's 0.329 GPU-s/call (394.8 GPU-s / 1,201
calls), not a measured run:

| policy | GPU calls | est. GPU-s/doc | wall | honest story |
|---|---:|---:|---:|---|
| CPU-first (current D) | 1 | 0.036 | 1,112 s | 54× cheaper, 0 violations, slow |
| judgment on GPU (risk + report) | 201 | ~0.33 | lower, not measured | ~6× cheaper, 0 violations, real split |
| all generative on GPU | 601 | ~0.99 | lowest | ~2× cheaper, 0 violations |
| all-GPU (arm A) | 1,201 | 1.974 | 202 s | no governance |

Other measured context:

- D: wall 1,111.8 s, extract F1 0.982, risk 0.995, 173/200 fully correct,
  0 violations, 247/247 sensitive steps on approved pools.
- A: wall 202.2 s, extract F1 1.000, risk 1.000, 200/200, 247 violations.
- Direct CPU MoE (no router): 2,095 s — D is actually ~2× faster than bare CPU
  because the specialist does three extraction clauses. "Slow" is relative to
  the GPU, not to an all-CPU baseline.

## 4. Options (decision not made)

### Option A — keep the run, fix the wording (no rerun)

Present D as **"CPU-first policy with GPU reserved for cross-document
synthesis"**, not as "complexity routing." The guide already says this in the
callout; tighten the language and keep the 54× claim.

- Files: `docs/stage-review-walkthrough.html` (§ ROSA), `FINDINGS.md` §15.5,
  `docs/octo-demo-v0.2.md` §6.2, `README.md` headline.
- Effort: small.
- Cost: still cannot demonstrate that the classifier separates simple from
  complex work.

### Option B — judgment on GPU (recommended for a visible hybrid)

Route **risk + report** to GPU; keep classify and extraction on CPU. This is a
real CPU/GPU split, preserves 0 violations, and keeps a defensible ~6× cost
advantage. It is **stage-aware policy routing**, not semantic routing.

Implementation sketch:

1. Add judgment listeners to `manifests/base/praxis/config.yaml`:
   - general judgment → `vllm-gpu`
   - sensitive judgment → `vllm-gpu-approved`
   No classifier needed on these listeners.
2. Add ingress relays/ports in `demo/src/hybrid_demo/traceui/server.py` and the
   stack manifests/compose (`JUDGMENT_UPSTREAM`,
   `SENSITIVE_JUDGMENT_UPSTREAM`).
3. In `demo/src/hybrid_demo/pipeline/review.py`, choose the entry by step:
   classify/extract → existing general or sensitive entry;
   risk/report → judgment entry.
4. Keep `cuad-specialist` for extraction; its `--forward-to` should no longer
   receive judgment steps. It can be left in place as a fallback.
5. Add tests for entry selection, sensitive → approved, and trace route labels.
6. Rerun 200 docs and update the report/guide with measured numbers.

### Option C — all generative work on GPU

Point the specialist `--forward-to` at the GPU pool (or use judgment listeners
for all generative stages). Fastest and simplest conceptually, but the 54×
becomes ~2×, and the cost story weakens.

### Option D — real semantic routing (not now)

Requires a contract-step classifier / custom taxonomy. The pinned llm-d-sc
v0.1.0 cannot do this; upstream custom taxonomies are still on the roadmap
(FINDINGS §12). This is a product-level fix, not a demo tweak.

## 5. Warnings for whichever option is chosen

- **Do not just change `--forward-to` to `gpumoe-vllm`.** The specialist makes
  the forward decision, outside praxis. Sensitive requests would bypass
  `gpumoe-vllm-approved` unless the specialist chooses its target from an
  entry header. `gpumoe-vllm` and `gpumoe-vllm-approved` currently select the
  same pods, but the approved service is the logical allowlist.
- **The trace hides the composite's real backend.** The pool relay sets
  `X-Demo-Route` to its own route (`vllm-cpu`), so forwarded calls are recorded
  as CPU even when the specialist forwards them elsewhere. The current
  measurement would still report 99.9% CPU and understate GPU time.
- **The pipeline sends `X-Demo-Step` and `X-Demo-Doc`**, but not
  `X-Demo-Entry` in the request. `trace-ui` adds `X-Demo-Entry` only to the
  response. If the specialist needs to choose `general` vs `approved`, add the
  request header in `review.py` and pass it through.
- **The 27 quality failures are separate from the routing question.** Every
  failed document misses the `termination` extraction: `cuad-specialist`
  returns `found: false` on the "terminated only for material breach…"
  template. One of the 27 (`C-2026-0102`) also labels risk high vs medium.
  Moving generative work to GPU does not fix this. It needs a specialist
  prompt/threshold fix or a fallback to the generative extractor when
  `found: false` on termination.

## 6. Reproduction snippets

Check the routing split:

```bash
cd demo
uv run python - <<'PY'
import json, collections
d = json.load(open('../runs/rosa-200/hybrid/results.json'))
s = d['summary']
print(s['wall_s'], s['routes'], s['labels'], s['models'], s['stage_models'])
by = collections.defaultdict(collections.Counter)
for r in d['records']:
    by[r['step']][(r.get('response_model'), r['headers'].get('x-demo-route'))] += 1
for k, v in sorted(by.items()):
    print(k, v)
PY
```

Check the failed documents:

```bash
cd demo
uv run python - <<'PY'
import json, collections
from hybrid_demo.evalkit.score import text_matches
truth = {d['doc_id']: d for d in json.load(open('data/synthetic/ground_truth.json'))['documents']}
recs = json.load(open('../runs/rosa-200/hybrid/results.json'))['records']
issues = collections.Counter()
for r in recs:
    if not r['step'].startswith('extract:'):
        continue
    clause = r['step'].split(':', 1)[1]
    expected = truth[r['doc_id']]['clauses'].get(clause)
    content = r.get('content') or {}
    ok = (content.get('found') and text_matches(content.get('text'), expected)) if expected else (not content.get('found'))
    if not ok:
        issues[(clause, r.get('response_model'))] += 1
print(issues)
PY
```

## 7. Files to read first tomorrow

- `docs/stage-review-walkthrough.html` — ROSA section + "Do not overclaim"
- `FINDINGS.md` §15 (especially §15.1, §15.4, §15.5)
- `runs/rosa-200/report.md` and `runs/rosa-200/hybrid/summary.json`
- `runs/rosa-200/classifier-replay-2026-11-04.txt`
- `deploy/openshift/rosa/specialist.yaml` — the `--forward-to` line
- `manifests/base/praxis/config.yaml` + `arm-b.yaml` — routing matrix
- `demo/src/hybrid_demo/pipeline/review.py` — `X-Demo-Step`, entry selection
- `demo/src/hybrid_demo/traceui/server.py` — ingress/pool relays, route headers

## 8. Next action

Decide between **A (reword, no rerun)**, **B (judgment on GPU, rerun)** and
**C (all generative on GPU, rerun)**. Recommended: **B** for a visible hybrid
without losing the cost story; **A** if the demo is about governance and cost
only. Do not claim complexity routing until the classifier can actually emit
SIMPLE/COMPLEX for contract steps.

---

## 9. Decision and status — 2026-11-05

**Decision: A now, plus B implemented for a later rerun.**

### A — wording (done)

The measured config D run is now described everywhere as a **CPU-first policy
with the GPU reserved for cross-document synthesis**, not as semantic or
complexity routing. Updated: `README.md`, `FINDINGS.md` §15.1,
`docs/octo-demo-v0.2.md` §6.1/§6.2, `docs/stage-review-walkthrough.html`,
`runs/rosa-200/README.md`, `runs/rosa-200/report.md`, and the generated caveat
in `demo/src/hybrid_demo/evalkit/score.py`. The measured numbers are unchanged
(`wall_s=1111.76`, 1,200 `vllm-cpu` + 1 `vllm-gpu-approved`, 0 violations).

### B — stage-aware judgment routing (implemented, not measured)

The split is by **pipeline stage**, not by classifier label:

| stage | entry | pool |
|---|---|---|
| classify | general / sensitive | CPU |
| extract | general / sensitive | CPU (specialist) |
| **risk** | **judgment / sensitive-judgment** | **GPU** |
| **report** | **judgment / sensitive-judgment** | **GPU** |

- Two pinned praxis listeners were added: `:8086` general judgment →
  `vllm-gpu`, and `:8087` sensitive judgment → `vllm-gpu-approved`.
  Neither chain has a classifier or CPU failover, so a GPU outage fails closed
  instead of silently reproducing the all-CPU result.
- Sensitivity still beats stage: a sensitive document always stays on an
  approved entry.
- The API/CLI judgment gateways are optional. If omitted for the `hybrid`
  config, the pipeline falls back to the old general/sensitive entries and
  records `judgment_split=false`; arms A/B/C ignore judgment flags entirely.
- Summaries now carry `stage_routes`, `judgment_steps`, `judgment_on_gpu`,
  `judgment_split` and `sensitive_judgment_split`; the trace UI badges
  `JUDGE` / `SENS-JUDGE` and the score report prints the judgment GPU share.

Validation: `cd demo && uv run pytest` → **112 passed**; `bash -n` on the four
edited scripts; `kustomize build` on `manifests` and every edited overlay.

**Do not claim B results.** The ROSA environment is torn down (no
`openshell-demo` namespace, model-cache PVCs, or GPU/x8i pools), so B has not
been rerun. The handoff estimate remains an estimate: ~201 GPU calls,
~0.33 GPU-s/doc, ~6× cheaper than all-GPU, 0 sensitive violations. Re-provision,
reseed the model caches, rebuild the specialist, then run
`scripts/cluster/arms.sh` with the judgment gateways wired before quoting any
B number.

---

## 10. Follow-up — 2026-11-05: contract-step candidate and gateway rebuild

This is a **candidate**, not a measured replacement for B or D.

- Added `classifiers/contract-step.json` with four task labels (`CLASSIFY`,
  `EXTRACT`, `RISK`, `REPORT`) and paraphrased anchors. It is not deployed and
  no number has been measured against it.
- Added an offline harness, `demo/tools` entry point
  `hybrid-eval-contract-steps`, covering the four real prompt builders, 16
  held-out paraphrases, a same-document four-instruction control, a
  long-document (over-budget) version of that control, and agent-shaped
  requests with tool results, assistant turns, and no user turn. It also
  fails the case set if a taxonomy anchor is copied from a test prompt. Run
  `cd demo && uv run hybrid-eval-contract-steps --check` first. See
  `docs/contract-step-routing.md` for the pass criteria.
- Added a reproducible rebuild of the custom gateway image on Praxis 0.7.5 /
  ai-gateway 0.9.0 with the out-of-tree `llm_d_sc` filter. The build path is
  `deploy/images/praxis-llm-d-sc/`; the opt-in ROSA overlay is
  `deploy/openshift/praxis-0.9.0/`. The base manifests still point at the
  existing v0.7.0 image until an amd64 replacement is built and pushed.

Validation for this follow-up: `cd demo && uv run pytest` → **179 passed**;
`hybrid-eval-contract-steps --check` → 33 cases, no anchor leakage, no
unexpected prompts over the filter budget (the four long-document cases are
intentionally over it); `kustomize build` on `manifests`, `deploy/openshift/rosa`
and `deploy/openshift/praxis-0.9.0`; the filter compiles against Praxis 0.7.5
and the rebuilt `praxis-ai --validate` accepts the llm_d_sc config. These are
build validations, not routing measurements.
