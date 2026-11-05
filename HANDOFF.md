# Handoff — config names, routes, models, and hardware

Last updated: **2026-11-05**. All current changes are uncommitted.

This document exists because the names are overloaded. A run directory, a
config name, a route name, a model name, and actual hardware are **five
different things**, and the same word can appear in more than one layer. Read
this before changing names again.

## Read this first

1. **`gpu-small` is a routing-policy name, not hardware.** On ROSA, the route
   `vllm-gpu-small` is backed by **Qwen2.5-1.5B on CPU**. On the Linux/g5 real
   profile it is a small **GPU** model. The name is historical.
2. **`all-gpu` is also a routing-policy name, not a hardware promise.** It
   means "send every request to the route called `vllm-gpu`". On ROSA that
   route is 4×A10G; on the earlier SNO stack every route, including
   `vllm-gpu`, was CPU-served.
3. **`all-gpu` is not a direct run.** It still goes through trace-ui and
   praxis. Direct means one bare endpoint with no relay, praxis, label, pool
   choice, or trace. Direct GPU is `runs/direct/gpu-moe`.
4. **Always read the pool map for hardware.** For a run, that is
   `results.json` → `summary.pools`; for ROSA deploy metadata, that is
   `deploy/openshift/rosa/pools.json`. Do not infer hardware from a route name.
5. **The current run directory is `runs/rosa-200/hybrid`** (old `arm-D`). The
   default dashboard uses A = that run, B = `runs/direct/gpu-moe`.

## Five things that look like names

| Layer | Example | Where to read it |
|---|---|---|
| Run directory | `runs/rosa-200/hybrid` | the path; maps to one recorded run |
| Routing policy / config | `hybrid` (old D) | `summary.config`; legacy `summary.arm` |
| Route / cluster | `vllm-cpu`, `vllm-gpu-approved` | `x-demo-route` response header; `summary.routes` |
| Served model | `Qwen3.5-35B-A3B`, `cuad-specialist` | response `model`; `summary.stage_models` |
| Actual hardware | `4× A10G`, `CPU x8i.24xlarge` | `summary.pools[route].hardware` |

A route can be composite: the route name says where praxis sent the request,
but the response model can be a specialist behind that route. Route ≠ model.

## The four routing configs

These are the policies praxis applies. They describe **routing behavior**, not
hardware.

| Config | Old key | Behavior | Routes used |
|---|---|---|---|
| `all-gpu` | A | Every request → `vllm-gpu`. No classifier, no CPU, no failover. | `vllm-gpu` |
| `gpu-small` | B (`gpu-split`) | Classifier: SIMPLE/MEDIUM → `vllm-gpu-small`; COMPLEX/REASONING → `vllm-gpu`. On failure, fail over to either route. | `vllm-gpu-small`, `vllm-gpu` |
| `round-robin` | C | Alternate across CPU and GPU with no classifier. | `vllm-cpu`, `vllm-gpu` |
| `hybrid` | D (`matrix`) | General entry: SIMPLE/MEDIUM → `vllm-cpu`; COMPLEX/REASONING → `vllm-gpu`. Sensitive entry: CPU or `vllm-gpu-approved` only; fail closed. | `vllm-cpu`, `vllm-gpu`, `vllm-gpu-approved` |

Config manifests:

- `manifests/base/praxis/arm-a.yaml`
- `manifests/base/praxis/arm-b.yaml`
- `manifests/base/praxis/arm-c.yaml`
- `manifests/base/praxis/config.yaml` (hybrid)

The filenames still use `arm-*` because they are both the internal config
files and historical names. Do not rename them without a migration plan.

## Actual hardware is deployment-specific

| Route / cluster | ROSA (`models-arch-ocp`) | Linux/g5 `real` profile | Earlier SNO |
|---|---|---|---|
| `vllm-cpu` | Qwen3.5-35B-A3B, CPU x8i.24xlarge | Qwen2.5-1.5B, CPU | CPU-served |
| `vllm-gpu` | Qwen3.5-35B-A3B, 4×A10G (g5.12xlarge, TP=4) | Qwen2.5-7B, GPU | CPU-served |
| `vllm-gpu-approved` | Qwen3.5-35B-A3B, same 4×A10G | Qwen2.5-7B, GPU | CPU-served |
| `vllm-gpu-small` | **Qwen2.5-1.5B, CPU x8i.24xlarge** | **Qwen2.5-1.5B, GPU** | CPU-served |
| `specialist` | CUAD RoBERTa-large QA, CPU (composite relay) | not part of A/B/C/D | not part of A/B/C/D |

Sources:

- ROSA: `deploy/openshift/rosa/pools.json` and each run's `summary.pools`
- ROSA "all CPU" variant: `deploy/openshift/rosa/pools-cpu.yaml`
- Linux/g5 real profile: `deploy/linux/docker-compose.yml` (`profiles: ["real"]`)
- SNO: `FINDINGS.md` §1 — all pools were CPU-served
- Fake/simulated profile: names are tiers; there is no real hardware

The presenter guide has the on-screen version of this warning in its
"Pool vs hardware" row: a `vllm-gpu` chip is a tier name, not proof of a GPU.

**Do not write a sentence like "all-gpu uses a GPU" without naming the
environment.** It is true on ROSA and false on SNO. Write "on ROSA,
`all-gpu` routes to the 4×A10G pool" or "on SNO, the same route was
CPU-served."

## What actually happened on the ROSA 200-document run

These numbers come from `runs/rosa-200/report.json`.

| Config | Calls by route | Extract F1 | Risk acc | Wall s | Recorded GPU-s/doc | Note |
|---|---|---:|---:|---:|---:|---|
| `all-gpu` | 1201 × `vllm-gpu` | 1.000 | 1.000 | 202.19 | 1.974 | quality reference; all GPU |
| `gpu-small` | 1200 × `vllm-gpu-small`, 1 × `vllm-gpu` | 1.000 | **0.395** | 438.92 | 2.218 | 1.5B answered almost everything |
| `round-robin` | 600 × `vllm-gpu`, 601 × `vllm-cpu` | 0.992 | 1.000 | 612.91 | 2.855 | no classifier |
| `hybrid` | 1200 × `vllm-cpu`, 1 × `vllm-gpu-approved` | 0.982 | 0.995 | 1111.76 | **0.036** | 0 sensitive violations |

Why `gpu-small` collapsed: llm-d-sc labels almost every per-document contract
step **MEDIUM**. In config B, MEDIUM goes to the small model. So 1200 of 1201
calls went to Qwen2.5-1.5B; only the single cross-document report
(**REASONING**) went to the 35B GPU. The small model answered the risk steps
and risk accuracy fell to 0.395.

The 2.218 GPU-s/doc figure is **recorded under the route's GPU label**, even
though the pool map for that run says the `vllm-gpu-small` backend was
CPU-served. Treat cost numbers for that route with care; the route name is
doing the accounting, not the hardware.

Why `hybrid` works: the same MEDIUM steps go to the **large 35B model on CPU**,
not to the 1.5B. Only the report goes to the approved GPU. That keeps quality
(F1 0.982, risk 0.995) while cutting recorded GPU time to 0.036 GPU-s/doc. It is
slower in wall-clock time (1112 s vs 202 s) because CPU inference is slower.

## Direct vs through-stack

### Direct run

- One endpoint for the entire run: `--endpoint http://host:port/v1`.
- The batch runner does the CPU-side work: reads documents, screens for PII,
  builds prompts, sends HTTP, parses, scores.
- The endpoint does **all** model inference: classify, four extracts, risk,
  and one report per run.
- No trace-ui relay, no praxis, no label, no pool choice, no trace id.
- Current runs:
  - `runs/direct/gpu-moe` — 35B-A3B, 4×A10G, endpoint `127.0.0.1:19200/v1`
  - `runs/direct/cpu-moe` — 35B-A3B, CPU, `127.0.0.1:19100/v1`
  - `runs/direct/cpu-small` — 1.5B, CPU, `127.0.0.1:19300/v1`

The direct path is **one endpoint per run**, not "one hardware target" in the
abstract: GPU runs are all-GPU, CPU runs are all-CPU. There is no mixing.

### Through-stack run

- Six stops: batch runner → ingress relay → praxis → pool relay → pool → model.
- Two trace-ui relay layers: ingress relays on `:8080`/`:8083` before praxis,
  per-pool relays on `:8081`/`:8082`/`:8084`/`:8085` after praxis.
- The batch runner screens PII and chooses general or sensitive entry.
- Praxis asks llm-d-sc for a complexity label and chooses a route.
- Only the vLLM/specialist backend runs inference.
- `all-gpu`, `gpu-small`, `round-robin`, and `hybrid` are **all through-stack**.

## Run directory map

| Path | Meaning |
|---|---|
| `runs/rosa-200/all-gpu` | through-stack config `all-gpu`, 200 docs |
| `runs/rosa-200/gpu-small` | through-stack config `gpu-small`, 200 docs |
| `runs/rosa-200/round-robin` | through-stack config `round-robin`, 200 docs |
| `runs/rosa-200/hybrid` | through-stack config `hybrid`, 200 docs (old `arm-D`) |
| `runs/direct/gpu-moe` | direct, one GPU endpoint, 35B-A3B |
| `runs/direct/cpu-moe` | direct, one CPU endpoint, 35B-A3B |
| `runs/direct/cpu-small` | direct, one CPU endpoint, 1.5B |
| `runs/cuad-test/arm-*` | historical CUAD evidence; ids preserved |
| `runs/simulated/arm-*`, `runs/gpu-measured/arm-*`, `runs/cuad-mixed/arm-*` | historical evidence; ids preserved |

See `runs/README.md` for the short version.

## Current CLI and scripts

One entry point:

```bash
# Direct: one endpoint, no gateway.
uv run hybrid-run --target direct \
  --endpoint http://127.0.0.1:19200/v1 \
  --data data/synthetic --out ../runs/direct/gpu-moe

# Through-stack: relays + praxis.
uv run hybrid-run --target gateway \
  --gateway http://relay:8080 \
  --sensitive-gateway http://relay:8083 \
  --config hybrid \
  --data data/synthetic --out ../runs/rosa-200/hybrid
```

- Primary config flag: `--config all-gpu|gpu-small|round-robin|hybrid`.
- Legacy `--arm A|B|C|D` still works but is hidden from help.
- Aliases: `gpu-split` → B, `matrix` → D, lowercase letters accepted.
- `hybrid-direct-arm` and `hybrid-review` remain aliases for the low-level
  entry points.
- Linux routing swap: `scripts/linux/set_config.sh hybrid` (preferred) or
  `scripts/linux/set_arm.sh hybrid` (legacy filename).
- Shell name/file mapping: `scripts/configs.sh`.
- The dashboard default is `RUN=../runs/rosa-200/hybrid`,
  `COMPARE=../runs/direct/gpu-moe`.

### A/B orientation

The default dashboard and the CUAD example in the guide use **opposite** A/B
orders:

- Default `make dashboard-up`: A = `runs/rosa-200/hybrid` (through-stack),
  B = `runs/direct/gpu-moe` (direct GPU).
- Guide CUAD example: A = direct run `arm-direct-moe-gpu-v2`,
  B = through-stack `arm-D-specialist` (historical id; config hybrid).

Do not say "A is direct" or "B is through-stack" without naming the run.

## What changed on 2026-11-04

- Renamed ROSA config directories:
  - `arm-A` → `all-gpu`
  - `arm-B` → `gpu-small`
  - `arm-C` → `round-robin`
  - `arm-D` → `hybrid`
- Added `hybrid-run --target direct|gateway` in
  `demo/src/hybrid_demo/pipeline/run.py`.
- Added `--config` to `review.py`; hid `--arm`; added name/alias resolution.
- Added `summary.config`; kept `summary.arm` as the internal key.
- Added `config` to the UI payload; old runs map `arm` → `config` in
  `demo/src/hybrid_demo/ui/payload.py`.
- Updated dashboard run line, guide tables, guide A/B note, and both pipeline
  SVGs to use policy names and plain language.
- Rebuilt the direct and through-stack SVG diagrams.
- Updated Linux/ROSA scripts to use policy names and `hybrid-run`.
- Added `scripts/linux/set_config.sh` as the name-first wrapper; `set_arm.sh`
  remains the implementation behind it.
- Added `runs/README.md` and `runs/rosa-200/README.md`.
- Added naming notes to `README.md`, `FINDINGS.md`, and
  `docs/octo-demo-v0.2.md`.
- Added `demo/tests/test_run_cli.py` tests for target dispatch and aliases.
- Added/updated `docs/stage-review-walkthrough.html` sections explaining
  batch runner, endpoint, relay, praxis, pool, and config names.

Validation at handoff:

```text
103 passed
shell syntax OK
SVG OK
dashboard: http://127.0.0.1:8099/
A = runs/rosa-200/hybrid
B = runs/direct/gpu-moe
```

## What changed on 2026-11-05

Items 2-6 from the previous handoff's open list are done; item 1 (renaming
`all-gpu` / `gpu-small`) is still the open decision.

- Added `demo/src/hybrid_demo/configs.json` as the canonical metadata record:
  key, name, legacy mode, aliases, behavior, routes, manifest and governance.
  It deliberately says nothing about hardware.
- Added `demo/src/hybrid_demo/configs.py`; `review.py`, `ui/payload.py` and
  `evalkit/score.py` load names and aliases from it.
- `scripts/configs.sh` is now a thin adapter over the same JSON, so the shell
  and Python cannot disagree.
- Removed the inline config-name maps from `ui/index.html` and
  `docs/stage-review-walkthrough.html`. The payload exposes the catalog as
  `configs`; `GET /api/configs` serves it directly.
- The dashboard header now shows the recorded pool hardware from
  `meta.pool_hardware` (only the routes that answered) and says "hardware not
  recorded" when a run has no pool map. The guide shows the same line.
- Report and trace UI labels now say **config**, using `summary.config` and
  falling back to the recorded A/B/C/D key for old runs (the report maps that
  key to the policy name; the trace UI shows the key because it stays
  standalone). `report.json` keeps its historical `arms` key; direct rows read
  `direct/<pool-dir>`.
- `hybrid-score report` now accepts the documented repeated `--runs` form as
  well as one `--runs` followed by many directories.
- Regenerated `runs/rosa-200/report.md`, `.json` and `.html` from all four
  runs.
- Added `demo/tests/test_configs.py`: alias coverage, legacy `--arm`, old-run
  naming, catalog exposure and pool-map hardware.

Operational hardening of the stage UI's live mode (same day, review follow-up):

- `hybrid-ui` gained `--live-token` (or `HYBRID_UI_LIVE_TOKEN`): when set, the
  POST routes `/api/live`, `/api/live/stream` and `/api/corpus/stream` require
  it as `X-Live-Token` or `Authorization: Bearer`, 401 otherwise. Without a
  token and with `--endpoint`, startup prints a stderr warning that live mode
  is unauthenticated. A non-loopback `--host` with live mode refuses to start
  unless the token is set or `--allow-insecure-live` opts in (lab use). The
  page asks for the token once on the first 401 and keeps it in
  sessionStorage.
- Archive uploads are size-capped on both sides of the base64 decode
  (`corpus.decode_archive`, `MAX_ARCHIVE_BYTES` = 16 MiB, 413 on oversize),
  so a huge `archive.data` cannot OOM the process before the per-member caps
  run.
- Submitted documents are held in a bounded FIFO store (`server.LiveDocs`,
  128 documents / 16M characters, oldest evicted, ids stay unique), so
  `live_docs` no longer grows for the process lifetime.
- The presenter's guide states that live submissions are direct runs — no
  PII entry, labels or sensitive-pool routing — and that the governed path
  for fresh documents is `hybrid-run --target gateway`.
- `demo/Makefile` wraps the three ways to start the UI: `make dashboard-up`
  (replay only, 127.0.0.1), `make dashboard-live` (adds live runs against
  `ENDPOINT`, default a localhost vLLM), and
  `make dashboard-live HOST=0.0.0.0 LIVE_TOKEN=...` (live on the network,
  token-gated; `HYBRID_UI_LIVE_TOKEN` also works).
- Added `demo/tests/test_ui_live_auth.py` and `decode_archive` tests in
  `demo/tests/test_ui_corpus.py` (193 tests total).

Validation at handoff:

```text
135 passed
ruff OK (touched files)
shell syntax OK
SVG OK
dashboard: http://127.0.0.1:8099/
A = runs/rosa-200/hybrid (config hybrid, arm D)
B = runs/direct/gpu-moe
/api/configs = all-gpu, gpu-small, round-robin, hybrid
```

## Open decisions

**Status 2026-11-05:** item 1 is still open. Items 2-6 were completed; the text
below is retained as the original rationale.

### 1. Decide whether `all-gpu` and `gpu-small` should be renamed again

They still imply hardware. A behavior-based set would be harder to misread:

| Current | Proposed | Why |
|---|---|---|
| `all-gpu` | `one-pool` or `single-route` | all requests to one through-stack route; no hardware claim |
| `gpu-small` | `small-first` | easy work to the small model, hard work to the large model |
| `round-robin` | `round-robin` | already behavioral |
| `hybrid` | `hybrid` (or `cpu-first`) | already established; optionally make the behavior explicit |

Recommendation: rename at least A and B to behavior names, keep every old name
and letter as an alias, and keep the route/cluster names (`vllm-gpu`,
`vllm-gpu-small`) unchanged for evidence compatibility. Do not rename
historical run directories.

If you decide not to rename, the minimum alternative is to show a hardware
badge everywhere the config name appears.

### 2. Make config metadata one source of truth

Today the mapping is duplicated in:

- `demo/src/hybrid_demo/pipeline/review.py` — `CONFIG_NAMES`, `CONFIG_ALIASES`
- `demo/src/hybrid_demo/ui/payload.py` — `CONFIG_NAMES`
- `demo/src/hybrid_demo/ui/index.html` — `CONFIG_NAMES`
- `docs/stage-review-walkthrough.html` — inline `configNames`
- `scripts/configs.sh` — shell name/key/file mapping

Tomorrow, add one canonical metadata file (for example `configs.json`) with
name, legacy aliases, behavior summary, and the route(s) used. Load it in
Python, expose it to the UI/guide (API or generated JS), and keep the shell
helper as a thin adapter. This prevents the next rename from leaving one page
behind.

### 3. Show actual hardware next to the config

The payload already exposes `meta.pools` with each route's model, hardware,
and deployment. The dashboard currently puts that hardware in the pool node's
hover title; make it visible. Add a small "what this run used" panel or header
line:

```text
config hybrid · route vllm-cpu (35B-A3B, CPU x8i) ·
route vllm-gpu-approved (35B-A3B, 4×A10G) · 0 sensitive violations
```

Read it from the recorded pool map, not from the config name. For runs without
a pool map, say "hardware not recorded" rather than guessing.

### 4. Update the report and trace UI labels

- `demo/src/hybrid_demo/evalkit/score.py` still uses "arm" table headers and
  prints `arm A/B/C/D` in report.md/report.html.
- The trace UI monitor/run selector still shows `arm` values.
- Map display names from `summary.config` when present and fall back to the
  `arm` letter for old runs. Keep historical run ids in the table if useful.

### 5. Regenerate the ROSA report after label changes

```bash
cd demo
uv run hybrid-score report \
  --runs ../runs/rosa-200/all-gpu \
  --runs ../runs/rosa-200/gpu-small \
  --runs ../runs/rosa-200/round-robin \
  --runs ../runs/rosa-200/hybrid \
  --data data/synthetic --out ../runs/rosa-200/report.md
```

Check that `report.md`, `report.json`, and `report.html` agree.

### 6. Add regression tests

- Every alias resolves to the same internal key.
- Old `--arm` still works.
- Existing runs with only `arm` show the right config name in the payload.
- A run with a pool map shows hardware from the pool map, not the route name.

## Do not break

- Do **not** change `summary.arm` values (A/B/C/D); they are recorded evidence.
- Do not rename historical run directories under `runs/cuad-test`,
  `runs/simulated`, `runs/gpu-measured`, or `runs/cuad-mixed`.
- Do not infer hardware from `vllm-gpu`, `vllm-gpu-small`, or `all-gpu`.
- Do not call `all-gpu` a direct run. Direct runs live under `runs/direct`.
- Keep `--arm`, letter aliases, `gpu-split`, and `matrix` working.
- Keep `set_arm.sh` working even if `set_config.sh` becomes the documented name.

## Quick verification

```bash
cd demo
uv run pytest -q
uv run hybrid-run --target gateway --help   # should show --config, not --arm
uv run hybrid-run --target direct --help

cd ..
for f in scripts/configs.sh scripts/linux/set_arm.sh \
         scripts/linux/set_config.sh scripts/linux/arms.sh \
         scripts/linux/demo.sh scripts/cluster/arms.sh \
         scripts/cluster/model-comparison.sh; do bash -n "$f" || exit 1; done
xmllint --noout docs/pipeline-direct.svg docs/pipeline-through-stack.svg \
                 docs/architecture-as-built.svg

cd demo && make dashboard-down && make dashboard-up
curl -s http://127.0.0.1:8099/api/run | python3 -c \
  'import json,sys; m=json.load(sys.stdin)["meta"]; print(m["run"], m["config"], m["arm"])'
```

Expected: `hybrid hybrid D`.

## One-sentence summary for tomorrow

Config metadata is now one file (`demo/src/hybrid_demo/configs.json`), the
dashboard and guide show the recorded pool hardware beside the config, and the
report/trace labels say config; the only naming decision left is whether to
rename `all-gpu` and `gpu-small` to behavior-only names (`one-pool`,
`small-first`) with every old name kept as an alias.
