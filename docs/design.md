# Design — the governed contract-intelligence stack

This is the design document: how the system is put together, why it is built
this way, the rules it follows, and where it is going. It consolidates
(2026-11-05) three documents whose content belonged here — an earlier
`HANDOFF.md` (names, routes, models, hardware), `docs/OPENSOURCE-REUSE.md`
(the upstream-composition policy) and `docs/contract-step-routing.md` (the
semantic-routing path); those files are preserved in git history at commit
`b76645b`, and the older decision log they absorbed lives at commit
`8798da3` (`DECISIONS.md`). A later, dated handoff may exist at the repo root;
this document is the durable design, not a handoff.

The companions: [README.md](../README.md) is the runbook (how to run it),
[FINDINGS.md](../FINDINGS.md) is the measurement record (what was measured),
and [stage-runbook.md](stage-runbook.md) is the one-page stage script.
Where those say *what happened*, this says *how it is designed and why*.

## 1. Purpose and thesis

A legal-ops demo, fully measured: contracts are screened for personal data,
clause extraction runs on a fine-tuned specialist (CPU, milliseconds per
clause, cannot hallucinate — it returns spans of the source document), risk
judgment and cross-document synthesis escalate to a Qwen3.5-35B-A3B MoE, and
every request is routed and audited by the Praxis gateway. The two design
sentences:

- **Sensitivity is a constraint; batch is CPU-first.** Which pools *may*
  serve a request is a governance decision, enforced fail-closed; which pool
  *prefers* it is a policy decision, visible when it degrades.
- **The routing that actually happened is expressed directly.** There is no
  classifier in the request path: the measured runs showed the classifier
  labelled effectively everything one way, so the pins below encode the
  measured behaviour without the guesswork hop (§3.3).

## 2. System map and the naming model

This section exists because the names are overloaded. A run directory, a
config name, a route name, a model name, and actual hardware are **five
different things**, and the same word can appear in more than one layer.

### 2.1 Read this first

1. **`gpu-small` is a routing-policy name, not hardware.** On ROSA, the route
   `vllm-gpu-small` is backed by **Qwen2.5-1.5B on CPU**. On the Linux/g5
   real profile it is a small **GPU** model. The name is historical.
2. **`all-gpu` is also a routing-policy name, not a hardware promise.** It
   means "send every request to the route called `vllm-gpu`". On ROSA that
   route is 4×A10G; on the earlier SNO stack every route, including
   `vllm-gpu`, was CPU-served.
3. **`all-gpu` is not a direct run.** It still goes through trace-ui and
   praxis. Direct means one bare endpoint with no relay, praxis, label, pool
   choice, or trace. Direct GPU is `runs/direct/gpu-moe` (archived at
   `cfc52cc`; recover with `git checkout cfc52cc -- runs/direct/gpu-moe`).
4. **Always read the pool map for hardware.** For a run, that is
   `results.json` → `summary.pools`; for ROSA deploy metadata, that is
   `deploy/openshift/rosa/pools.json`. Do not infer hardware from a route
   name.
5. **The current run directory is
   `runs/rosa-200-2026-11-10-praxis-routes/hybrid`** (rebuilt two-entry graph,
   old config key `D`). The default dashboard uses A = that run, B = the
   same-run all-gpu control. The pre-rebuild 2026-11-09 record
   (`runs/rosa-200-2026-11-09-cpu-term-rerun/hybrid`) and its controls remain
   in the tree for comparison; they measured the composite `vllm-cpu` route.

### 2.2 Five layers that look like names

| Layer | Example | Where to read it |
|---|---|---|
| Run directory | `runs/rosa-200-2026-11-10-praxis-routes/hybrid` | the path; maps to one recorded run |
| Routing policy / config | `hybrid` (old D) | `summary.config`; legacy `summary.arm` |
| Route / cluster | `vllm-cpu`, `vllm-gpu-approved` | `x-demo-route` response header; `summary.routes` |
| Served model | `Qwen3.5-35B-A3B`, `cuad-specialist` | response `model`; `summary.stage_models` |
| Actual hardware | `4× A10G`, `CPU x8i.24xlarge` | `summary.pools[route].hardware` |

A route names one model endpoint. Since the 2026-11-10 rebuild there is no
composite route: the CUAD specialist and the CPU MoE are separate praxis
routes, so the route count is also a model count. Route ≠ hardware still
holds (a route can name a CPU-served pool), but route = endpoint.

#### What serves each pipeline step (rebuilt stack)

The pipeline sends `X-Demo-Step` and chooses only the general or sensitive
entry. Praxis matches the step against the route table in
`manifests/base/praxis/config.yaml`; the specialist is a model endpoint, not
a router.

| Step | Route | Served by | Response `model` |
|---|---|---|---|
| `classify` | `vllm-cpu` | vLLM CPU (Qwen3.5-35B-A3B MoE) | `Semantic-Qwen` |
| `extract:termination` | `cuad-specialist` | RoBERTa-large QA (cause follow-up stays inside the step) | `cuad-specialist` |
| `extract:governing_law` | `cuad-specialist` | RoBERTa-large QA | `cuad-specialist` |
| `extract:liability_cap` | `cuad-specialist` | RoBERTa-large QA | `cuad-specialist` |
| `extract:data_protection` | `vllm-cpu` | vLLM CPU (CUAD has no data-protection category) | `Semantic-Qwen` |
| `risk` | `vllm-gpu` (general) / `vllm-gpu-approved` (sensitive) | vLLM GPU (`gpumoe-vllm`, 4×A10G, TP=4) | `Semantic-Qwen` |
| `report` (once per run) | `vllm-gpu` / `vllm-gpu-approved` | same GPU backend | `Semantic-Qwen` |

The PII screen is **not** a row in this table: it runs before the entry is
chosen. The opt-in served shape calls `screen:pii` on the control-plane
listener (`:8086`), which routes only to `jev-cpu`; the 2026-11-10 run used
the regex screen and does not include that hop (see §3.1 and FINDINGS §16.9).

**The quoted 200-doc measurement is the rebuilt graph.** In
`runs/rosa-200-2026-11-10-praxis-routes/hybrid`, `stage_routes` shows
`cuad-specialist: 600` and `vllm-cpu: 400` as separate routes; the old
`runs/rosa-200-2026-11-09-cpu-term-rerun/hybrid` remains in the tree as the
pre-rebuild comparison, where `vllm-cpu: 1000` was 600 RoBERTa passes plus
400 forwarded Qwen calls. Both records stay valid for the routing that
produced them. Evidence per run is `summary.stage_routes` and
`summary.stage_models`; the specialist `/health` reports only
`{"model": "/opt/cuad-model/roberta-large"}`.

### 2.3 The four routing configs

These are the policies praxis applies. They describe **routing behavior**,
not hardware.

| Config | Old key | Behavior | Routes used |
|---|---|---|---|
| `all-gpu` | A | Every request → `vllm-gpu`. No CPU, no failover. | `vllm-gpu` |
| `gpu-small` | B (`gpu-split`) | Every request → `vllm-gpu-small` (historically label-routed; the classifier labelled everything MEDIUM, so the pin reproduces the measured routing). On failure, fail over to either GPU pool. | `vllm-gpu-small`, `vllm-gpu` |
| `round-robin` | C | Alternate across CPU and GPU. | `vllm-cpu`, `vllm-gpu` |
| `hybrid` | D (`matrix`) | Two entries. Inside each, the router table sends CUAD clause extraction to `cuad-specialist`, classify/data-protection to `vllm-cpu`, and risk/report to a GPU route (`vllm-gpu` general, `vllm-gpu-approved` sensitive). The general entry degrades to `vllm-gpu`; the sensitive entry degrades only to `vllm-gpu-approved` (fail closed). | `cuad-specialist`, `vllm-cpu`, `vllm-gpu`, `vllm-gpu-approved` |

Config manifests: `manifests/base/praxis/arm-a.yaml`, `arm-b.yaml`,
`arm-c.yaml`, and `config.yaml` (hybrid). The filenames still use `arm-*`
because they are both the internal config files and historical names. Do not
rename them without a migration plan.

`config.yaml` also carries the `screen-gateway` control-plane listener and
the `jev-cpu` cluster described in §3.1. The A/B/C configs do not have that
listener, so the screen route is a property of the hybrid stack only; the
trace-ui screen ingress is therefore useful only with `config.yaml`.

### 2.4 Actual hardware is deployment-specific

| Route / cluster | ROSA (`<redacted-cluster>`) | Linux/g5 `real` profile | Earlier SNO |
|---|---|---|---|
| `cuad-specialist` | CUAD RoBERTa-large QA, CPU x8i.24xlarge (2 cores) | routed to the CPU pool (no RoBERTa service) | not part of A/B/C/D |
| `vllm-cpu` | Qwen3.5-35B-A3B, CPU x8i.24xlarge | Qwen2.5-1.5B, CPU | CPU-served |
| `vllm-gpu` | Qwen3.5-35B-A3B, 4×A10G (g5.12xlarge, TP=4) | Qwen2.5-7B, GPU | CPU-served |
| `vllm-gpu-approved` | Qwen3.5-35B-A3B, same 4×A10G | Qwen2.5-7B, GPU | CPU-served |
| `vllm-gpu-small` | **Qwen2.5-1.5B, CPU x8i.24xlarge** | **Qwen2.5-1.5B, GPU** | CPU-served |
| `jev-cpu` | Laya English (Jev-compatible), CPU x8i.24xlarge (2 cores, opt-in screen) | baked Jev/Laya image, CPU | not part |

Sources: `deploy/openshift/rosa/pools.json` and each run's `summary.pools`;
the ROSA all-CPU variant (`deploy/openshift/rosa/pools-cpu.yaml`); the
Linux/g5 real profile (`deploy/linux/docker-compose.yml`,
`profiles: ["real"]`); FINDINGS §1 for the SNO; the fake/simulated profile
has no real hardware at all. The presenter guide
(`docs/stage-review-walkthrough.html`) carries the on-screen version of this
warning in its "Pool vs hardware" row.

**Do not write a sentence like "all-gpu uses a GPU" without naming the
environment.** It is true on ROSA and false on SNO. Write "on ROSA,
`all-gpu` routes to the 4×A10G pool" or "on SNO, the same route was
CPU-served."

### 2.5 Direct vs through-stack

**Direct run** — one endpoint for the entire run
(`--endpoint http://host:port/v1`). The batch runner does the CPU-side work
(reads documents, screens PII, builds prompts, sends HTTP, parses, scores);
the endpoint does all model inference. No relay, praxis, label, pool choice
or trace id. Archived direct runs (recover from `cfc52cc`): `runs/direct/gpu-moe`
(35B-A3B, 4×A10G), `runs/direct/cpu-moe` (35B-A3B, CPU), `runs/direct/cpu-small`
(1.5B, CPU).
The direct path is one endpoint per run, not "one hardware target" in the
abstract: GPU runs are all-GPU, CPU runs are all-CPU, no mixing.

**Through-stack run** — six stops: batch runner → ingress relay → praxis →
pool relay → pool → model. Two trace-ui relay layers: ingress relays on
`:8080`/`:8083` before praxis, per-pool relays on
`:8081`/`:8082`/`:8084`/`:8085` after it. The batch runner screens PII and
chooses the general or sensitive entry; praxis routes by pinned policy
(§3); only the vLLM/specialist backend runs inference. All four configs are
through-stack.

### 2.6 Run directory map

| Path | Meaning |
|---|---|
| `runs/rosa-200-2026-11-10-praxis-routes/hybrid` | current through-stack `hybrid`, 200 docs — rebuilt two-entry / `X-Demo-Step` graph, 0 violations |
| `runs/rosa-200-2026-11-10-praxis-routes/all-gpu` | same-run `all-gpu` control, 200 docs (11 transport timeouts) |
| `runs/rosa-200-2026-11-10-praxis-routes/gpu-small` | same-run `gpu-small` matrix row |
| `runs/rosa-200-2026-11-10-praxis-routes/round-robin` | same-run `round-robin` matrix row |
| `runs/rosa-200-2026-11-09/all-gpu` | pre-rebuild through-stack `all-gpu`, 200 docs (same-day §15.9 copy) |
| `runs/rosa-200-2026-11-09/gpu-small` | through-stack config `gpu-small`, 200 docs (same-day §15.9 copy) |
| `runs/rosa-200-2026-11-09/round-robin` | through-stack config `round-robin`, 200 docs (same-day §15.9 copy) |
| `runs/rosa-200-2026-11-09-cpu-term-rerun/hybrid` | pre-rebuild through-stack `hybrid`, 200 docs (old `arm-D`), composite `vllm-cpu` route |
| `runs/rosa-200-2026-11-09-cpu-term/hybrid` | archived at `cfc52cc` — same policy, first measurement (198/200; two risk flips documented as serving variance) |
| `runs/rosa-200-2026-11-09-gpu-term/hybrid` | archived at `cfc52cc` — measured GPU-termination alternative, 200 docs (not the current policy) |
| `runs/direct/gpu-moe` | archived at `cfc52cc` — direct, one GPU endpoint, 35B-A3B |
| `runs/direct/cpu-moe` | archived at `cfc52cc` — direct, one CPU endpoint, 35B-A3B |
| `runs/direct/cpu-small` | archived at `cfc52cc` — direct, one CPU endpoint, 1.5B |
| `runs/cuad-test/arm-*` | CUAD appendix evidence; ids preserved |

Archived paths are not in the working tree; recover one with
`git checkout cfc52cc -- <path>`.

See `runs/README.md` for the short version.

### 2.7 CLI surface

One entry point, `hybrid-run`:

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

- Primary config flag: `--config all-gpu|gpu-small|round-robin|hybrid`;
  legacy `--arm A|B|C|D` still works but is hidden from help. Aliases:
  `gpu-split` → B, `matrix` → D, lowercase letters accepted.
- `hybrid-direct-arm` and `hybrid-review` remain aliases for the low-level
  entry points.
- The config catalog is one file, `demo/src/hybrid_demo/configs.json`
  (key, name, legacy mode, aliases, behavior, routes, manifest, governance —
  deliberately nothing about hardware); `review.py`, `ui/payload.py`,
  `evalkit/score.py` and `scripts/configs.sh` all load from it, so the shell
  and Python cannot disagree.
- Linux routing swap: `scripts/linux/set_config.sh hybrid` (preferred) or
  `scripts/linux/set_arm.sh hybrid` (legacy filename).
- A/B orientation: the default dashboard uses A =
  `runs/rosa-200-2026-11-10-praxis-routes/hybrid` (through-stack), B =
  `runs/rosa-200-2026-11-10-praxis-routes/all-gpu` (through-stack); the
  guide's CUAD example uses a direct/gateway pair. Do not say "A is direct"
  without naming the run.

## 3. Routing and governance design

### 3.1 Two review entries, one work-to-pool table, plus a screen control plane

Praxis exposes **two review listeners**. The pipeline chooses only the data
classification: the PII screen sends personal-data documents to the sensitive
entry, everything else to general. Pool choice happens inside praxis and
nowhere else. The screen is pluggable (`pii.py`); the measured default is the
regex. A Jev-compatible zero-shot adapter (`HYBRID_PII_DETECTOR=jev`, local
Laya or `HYBRID_JEV_URL`) was added on 2026-11-11 and benchmarked offline; it
is opt-in and not on the measured path (FINDINGS §16.7).

The screen itself runs *before* the pipeline can choose either review entry,
so `config.yaml` also has a third, **control-plane** listener on `:8086`.
It accepts `screen:pii` (and the optional `decide:cause`) and pins both to
the CPU-only `jev-cpu` cluster; it has no failover. It is not a review entry
and does not relax the two-entry rule: a failed screen is a hard error, and
`pii.is_sensitive()` fails closed to the sensitive review entry. The screen
request carries its own `screen` API key and the served model name only so
the model-scoped PPE policy route matches; the Jev service ignores the model
field. The `screen` detector's `HYBRID_PII_SCREEN_URL` points at the
trace-ui screen ingress (`:8086`), which records `entry=screen` and forwards
to the Praxis listener. Because the screen body is the document text,
`entry=screen` is treated like a sensitive entry for trace content storage:
the default database keeps the routing metadata but not the body. The
measured 2026-11-10 run used the regex screen and does not include this path
(FINDINGS §16.9).

Inside each entry, the `router` filter matches `X-Demo-Step` and sets the
cluster (exact values, case-sensitive; more header constraints win over the
bare path prefix):

| `X-Demo-Step` | General entry | Sensitive entry |
|---|---|---|
| `extract:termination`, `extract:governing_law`, `extract:liability_cap`, `extract:audit_rights` | `cuad-specialist` | `cuad-specialist` |
| `extract:data_protection` | `vllm-cpu` | `vllm-cpu` |
| `classify` | `vllm-cpu` | `vllm-cpu` |
| `risk`, `report` | `vllm-gpu` | `vllm-gpu-approved` |
| anything else | `vllm-cpu` | `vllm-cpu` |

The screen control plane has its own, narrower table:

| `X-Demo-Step` | Screen listener `:8086` |
|---|---|
| `screen:pii` | `jev-cpu` |
| `decide:cause` | `jev-cpu` |
| anything else (screen-only fallback) | `jev-cpu` |

The general entry fails over to `vllm-gpu`; a judgment request that was
already on `vllm-gpu` retries the same cluster and then fails rather than
silently reproducing the all-CPU result. The sensitive entry fails over only
to `vllm-gpu-approved`, so it fails closed when that pool is down. The
visible "degraded" signal is the failed hop on a completed request, which the
trace UI badges as a fallback (praxis does not emit `X-Praxis-Fallback` on
proxied failovers — FINDINGS §7; the header is only set on the explicit
failover step).

The `X-Demo-Step` value is declarative task metadata, not an authenticated
claim: the PPE policy checks that the key is known, but it does not bind a
key to a step. A client with any valid key can ask for another step's route.
For the demo the pipeline is trusted and the keys are fixed demo credentials;
binding key scope to step is a Phase 2 policy change, not a property of this
build.

Praxis reaches the pools only through the trace-ui relays (per-pool recording
hops), and SQLite behind them is the audit trail, exported as
`audit.jsonl`.

Every entry also runs the praxis `policy` filter — the Praxis Policy Engine
(PPE) — ahead of its router. The pipeline authenticates each step with a
per-step API key (`X-API-Key`; SHA-256 digests in `manifests/base/praxis/
api-keys.yaml`, plaintext in `demo/src/hybrid_demo/common.py`), so a request
without a known key fails closed at the gateway rather than reaching a pool.
The policy filter's identity gate runs ahead of the static-response body,
so this includes `GET /v1/models`: what used to be an unauthenticated
liveness answer now 401s, and the stack's own probes (up.sh, arms.sh,
smoke) present the eval key. (Conditioning the policy filter to exempt the
path is not an option — praxis rejects conditioned security filters at
load, correctly: a security filter that can be bypassed for non-matching
requests is a fail-open hazard.) The k8s pod probes are unaffected: they
use the admin port's `/healthy`, not the data path. The sensitive entries
additionally taint the session (`X-Session-Id`, one
per document) with a `sensitive` label on arrival: the label still originates
from the pipeline's screen — the trust model is unchanged — but the fact now
lives in gateway-owned state the client cannot rewrite. Nothing denies on
the label; any future rule on it is a measured decision.

Since image v0.9.1 the same policy layer emits **request records** (from
PPE's reference `audit/logger`, which the image host-registers because the
engine deliberately does not bundle it) and runs a **shadow PII screen**:
`validator/gci-pii-observe`, an observe-only scanner over `cmf.llm_input`
whose pattern catalog mirrors `pii.py` regex-for-regex. It never denies and
never rewrites; it logs one structured line per request, and
`hybrid-pii-agreement` joins those lines against a run's records to report
whether the gateway would have made the pipeline's screen call — the V19
groundwork. The comparison point is the classify-time observation; later
steps are context, because their prompts embed document text (a screened
document's risk prompt literally says "contains personal data"). Both
records land in the praxis container logs alongside the trace-ui SQLite
audit trail. One 0.4.1 limitation, recorded rather than worked around: the
published praxis-policy 0.4.1 cannot load a hookless audit *sink* beside
APL routes (the visitor rejects plugins no step names; fixed upstream on
the 0.5.0 line as policy#210), so the logger runs as a reached observer —
records for allowed traffic, no verdict field. Full verdict-and-deny
records arrive when a praxis release ships PPE 0.5.x.

The policy layer attributes and audits; it does not steer pools. PPE's
`restrict` effect is exactly the pool-constraint primitive this design
wants (narrow the router's candidate set, fail closed on empty), but the
praxis router does not consume it and the clusters carry no backend labels,
so containment remains the entry pinning and failover sets above. One
consequence of failing closed on unknown keys: the external
llm-d-batch-gateway's dispatch requests carry no `X-API-Key` and would be
denied by these entries — a known gap until that component can present a
key.


### 3.2 Why there is no classifier

The stack originally routed on llm-d-sc complexity labels. The measured
reality (ROSA, 200 documents, FINDINGS §15.5): the classifier labelled
**1,200 of 1,201 steps MEDIUM** — a taxonomy mismatch, not degradation;
its labels are prompt-difficulty, not per-step complexity. The one decision
the labels did drive (the 37k-character cross-document report scoring
REASONING → GPU) is exactly what the `X-Demo-Step` route table now does
directly, and better. So on 2026-11-05 the classifier was removed from the
default stack: the router table expresses the routing that actually
happened, without the gRPC hop, and the claim is "containment plus
GPU-first economics", never "the AI decides what is hard".

The removal was **built and tested**; the 2026-11-09 classifier-free matrix
in §3.3 is its re-measurement. Every §15 number before §15.8 came from the
classifier-in-path config. The classifier Deployment, its arm-B label-routed
config and the workflow jobs that published its image are preserved in git
history at commit `aa080e9`; the published image and mirrored modelcar stay
in the registry for §5's experiment.

### 3.3 The measured justification (ROSA 200-document matrix)

From `runs/rosa-200/report.json` (archived at `cfc52cc`; classifier-in-path;
historical — FINDINGS §15 has the full analysis):

| Config | Calls by route | Extract F1 | Risk acc | Wall s | Recorded GPU-s/doc | Note |
|---|---|---:|---:|---:|---:|---|
| `all-gpu` | 1201 × `vllm-gpu` | 1.000 | 1.000 | 202.19 | 1.974 | quality reference; all GPU |
| `gpu-small` | 1200 × `vllm-gpu-small`, 1 × `vllm-gpu` | 1.000 | **0.395** | 438.92 | 2.218 | 1.5B answered almost everything |
| `round-robin` | 600 × `vllm-gpu`, 601 × `vllm-cpu` | 0.992 | 1.000 | 612.91 | 2.855 | ungoverned hybrid |
| `hybrid` | 1200 × `vllm-cpu`, 1 × `vllm-gpu-approved` | 0.982 | 0.995 | 1111.76 | **0.036** | 0 sensitive violations |

Why `gpu-small` collapsed: the classifier labels almost every per-document
step MEDIUM, and in config B MEDIUM went to the small model — so 1,200 of
1,201 calls went to Qwen2.5-1.5B and risk accuracy fell to 0.395. The 2.218
GPU-s/doc figure is recorded under the route's GPU label even though that
run's pool map says the backend was CPU-served; the route name is doing the
accounting, not the hardware. Why `hybrid` works: the same steps go to the
large 35B on CPU, only the report goes to the approved GPU — quality kept
(F1 0.982, risk 0.995) with recorded GPU time cut to 0.036 GPU-s/doc, at
the cost of wall-clock (1112 s vs 202 s). Those recorded figures sum both
routes that front the same `gpumoe-vllm` pod; the rebuilt table below is the
current policy.

#### 2026-11-10 rebuilt-graph measurement

`runs/rosa-200-2026-11-10-praxis-routes` is the current through-stack record.
The hybrid arm runs termination extraction on the CPU specialist with a
conditional cause follow-up (canonical CUAD convenience question first; a
focused termination-for-cause question only when that returns no span and the
contract has cause language); judgment stays risk/report on the GPU route. All
arms: 200 documents, 1,201 steps, regex screen 41/0/159/0.

| config | wall | extract F1 | risk | report | fully correct | CPU share | sensitive approved | violations | errors | GPU-s/doc |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all-gpu | 601.22 | 0.993 | 0.975 | 0.764 | 189/200 | 0.0 | 0/247 | 245 | 11 | 0.043 |
| gpu-small | 457.63 | 1.000 | 0.395 | 0.278 | 77/200 | 0.0 | 0/247 | 247 | 0 | n/a |
| round-robin | 938.36 | 1.000 | 1.000 | 0.750 | 200/200 | 0.5 | 130/247 | 117 | 0 | n/a |
| **hybrid** | **839.90** | **1.000** | **1.000** | **0.750** | **200/200** | **0.833** | **247/247** | **0** | **0** | **n/a** |

GPU-s/doc counts a shared physical backend once:
`gpumoe-vllm-approved` and `gpumoe-vllm` are the same pod, so the scorer maxes
the group instead of summing it. The rebuilt hybrid arm's device time is
**`n/a`**, not zero: the per-pool relays did not expose vLLM's in-flight
gauges. The pre-rebuild 2026-11-09 table (0.22 GPU-s/doc hybrid vs 1.013
all-gpu) remains valid for that graph and must not be quoted for this one.
Judgment steps: 201/201 on GPU (159 `vllm-gpu`, 42 approved). Termination
extraction (200 steps) is on `cuad-specialist`; classify and data-protection
stay on `vllm-cpu`, so the route split is visible in `summary.stage_routes`
without a composite route. The all-gpu control's 11 transport timeouts are
part of the record; it is not a clean latency baseline.

### 3.4 Do not break

- Do **not** change `summary.arm` values (A/B/C/D); they are recorded evidence.
- Do not rename historical run directories under `runs/cuad-test`, or any
  archived path restored from `cfc52cc`.
- Do not infer hardware from `vllm-gpu`, `vllm-gpu-small`, or `all-gpu`.
- Do not call `all-gpu` a direct run. Direct runs are a separate mode; their
  archived examples live under `runs/direct` at `cfc52cc`.
- Keep `--arm`, letter aliases, `gpu-split`, and `matrix` working.
- Keep `set_arm.sh` working even if `set_config.sh` is the documented name.
- Do not reintroduce a forwarding branch in the specialist or any other
  model pod. A second hop inside the app bypasses praxis's route table, and
  the pool relay's `X-Demo-Route` header cannot show where the request
  actually went.
- Do not move step-to-pool choices into the pipeline. The app may declare
  `X-Demo-Step`; only `manifests/base/praxis/config.yaml` maps it to a pool.
- Do not treat `X-Demo-Step` as authenticated. The policy gate authenticates
  the caller, not the claimed step.

### 3.5 The agentic surface: MCP above, governance below

> Naming note: in this repository `oc get mcp` means OpenShift
> **MachineConfigPool**. This section means **Model Context Protocol (MCP)**.

The question “why didn’t you use an agent framework?” is the right one to
ask, and the answer is not “frameworks are unnecessary.” It is a question
about which layer owns which decision. Three layers are easy to conflate:

| Layer | In this repo | What it decides |
|---|---|---|
| **Workflow** | `pipeline/review.py`, `direct.py`, `batch.py`, `hybrid-run` | The stage order and prompts: screen, classify, extract, risk, report |
| **Agent runtime** | OpenClaw in the OpenShell sandbox (the live tool-calling trigger is still open) | Which tools to call, in what order, with what memory and planning |
| **Governance plane** | trace-ui ingress relays, praxis, per-pool relays, NetworkPolicies, sandbox egress | Which entry and pool may serve a request, when to fail closed, what is recorded |

The measured four-arm matrix uses the deterministic workflow as a
**reference client**. That is not a claim that an agent framework is
unsuitable; it is a controlled comparison. An agent that plans, retries or
reorders calls would confound the routing comparison, and the governance
result comes from the gateway, not the orchestrator. The reference client is
the golden path the agent is compared against, not the product boundary.

The product surface is the inverse composition, and it is a repeatable
methodology:

1. **Define the domain tool/resource catalog first** (MCP), not prompts
   buried inside a framework.
2. **Put the agent framework above MCP** for planning, memory and tool
   choice.
3. **Keep the policy/egress gateway below MCP**; the agent never reaches a
   model or a data source directly.
4. **Keep a deterministic reference client** for measurement and replay.
5. **Measure the reference, demo the agent, and label which is which.**

The intended catalog is small and domain-level:

| MCP surface | Kind | Calls |
|---|---|---|
| `contract://{doc_id}` | resource | document text and annotations |
| `review_document` | tool | the full deterministic review (screen → report) |
| `screen_document`, `extract_clause`, `assess_risk`, `build_report` | tools | one stage each, for agent composition |
| `search_clauses` | tool | retrieval over the clause index |

The first cut is implemented in `src/hybrid_demo/mcp/server.py` (console script
`hybrid-mcp`, plus the root launcher `mcp/contract_review_server.py`). It
serves `contract://{doc_id}`, `review_document`, `screen_document`,
`extract_clause`, `assess_risk` and `build_report`; `search_clauses` waits for
the clause index. The tools call the ingress relays, use the same per-step API
keys, and never reach a pool directly.

The MCP server is a **thin adapter**, not a second control plane. It runs
inside the sandbox/namespace, uses the same per-step API keys as the
pipeline, and calls the ingress relays. It must never call a pool's vLLM
endpoint directly: that would bypass the approved set and the audit trail.
The sandbox's egress policy still allows only praxis, so a compromised agent
can invoke the governed tools and nothing else.

What this buys: MCP hosts (OpenClaw, IDE assistants, other agent frameworks)
can discover the capability without importing this repository's prompts or
transport. What it does **not** buy: containment, fail-closed routing or
audit. Those remain the governance plane's job. An MCP gateway or registry
can add tool-level authentication and allowlists, but it still does not know
which model pool may serve a sensitive document; that decision stays in
praxis.

The stage answer to “why didn’t you use an agent framework?” is therefore:
*“We used a deterministic reference client for the measured comparison, and
an agent runtime above MCP for the interactive surface. The same tools and
the same gateway serve both; the difference is who chooses the next call.”*
The live agent trigger is the open item, not a rewrite of the measured path.

## 4. Composing with upstream open source

The short version: **the commodity mechanics are available from upstream;
the governance, routing and scoring decisions stay here.** Every adapter is
opt-in, an explicitly selected backend fails with an install/URL message
when absent, `auto` modes fall back and record which backend ran, and the
measured default path is unchanged.

### 4.1 Decision rule

A component was accepted only if it owns a mechanic we would otherwise
maintain, and only behind a flag/extra with the existing behaviour as the
default. A component was rejected as a *replacement* if it would:

- move a number without a paired A/B (PII screen, chunker, metric),
- bypass the praxis governance path (batch-to-pool, direct simulator),
- replace the audit trail instead of exporting alongside it, or
- take over a domain decision (pipeline prompts, rubric, scoring, composite
  routing, cache policy).

### 4.2 Audit and disposition

| area | hand-rolled before | upstream candidate | disposition |
|---|---|---|---|
| PII screen | 12 regexes in `classifier.py` | Presidio; llm-d-sc taxonomy; Jev/Laya | adapter; regex default |
| span scoring | containment in `score.py` | nervaluate | additive views; containment primary |
| significance | exact McNemar in `tools/mcnemar.py` | statsmodels | backend switch; exact fallback |
| ranking metrics | recall@k/MRR in `retrieval/evaluate.py` | ranx | NDCG/MAP backend; internal fallback |
| ingestion/chunking | builtin paragraph chunker | docling `HybridChunker` | adapter; builtin default |
| vector index | none (in-memory cosine) | LanceDB, bm25s | `PortfolioIndex`; memory default |
| semantic cache | in-memory dict/list | RedisVL + Redis | backend switch; policy stays ours |
| traces | SQLite + JSONL export | OpenTelemetry, Langfuse/Phoenix | OTel export additive; SQLite authoritative |
| RAG quality | none | RAGAS | optional judge metrics; lexical screen default |
| fake pool | `fakepool/server.py` | llm-d-inference-sim | proxy adapter; keep fake for cost/corruption |
| batch transport | Python runner only | llm-d-batch-gateway | client for `/v1/files`+`/v1/batches` |
| load generation | `tools/gpu_bench.py` | `vllm bench serve`, GuideLLM | command builder; pin versions |
| structured output | prompt asks for JSON | vLLM structured outputs | schema helper; off by default |

### 4.3 Defaults and activation

#### PII: Presidio, the llm-d-sc taxonomy and Jev/Laya

`src/hybrid_demo/pii.py` keeps the original regex list and returns
character-offset `Finding`s. `classifier.detect_sensitivity` delegates to
`pii.is_sensitive`, so the pipeline screen and the specialist cache stay in
sync; a detector failure fails **closed** (treated as sensitive).

```bash
cd demo
uv sync --extra pii
python -m spacy download en_core_web_lg   # Presidio's NER model
HYBRID_PII_DETECTOR=presidio uv run hybrid-review ...
```

- `regex` (default): byte-identical routing to the recorded arms.
- `presidio`: NER + checksum recognisers — people, companies, addresses,
  identifiers; the sensitive share will rise. There is no CUAD PII label,
  so do not quote precision/recall from CUAD.
- `taxonomy`: a small HTTP bridge in front of llm-d-sc's gRPC taxonomy.
  Contract: `POST <url> {"text": "..."}` → `{"labels": [...]}` or
  `{"findings": [{start,end,entity}]}`; set `HYBRID_PII_TAXONOMY_URL`.
  (llm-d-sc is no longer in the base stack — §3.2 — so this mode needs the
  experiment deployment from §5.5.)
- `jev`: one zero-shot System One question about actual identifiers, returned
  as a document-level probability (`noul`) and thresholded at 0.5. It uses a
  served endpoint (`HYBRID_JEV_URL`, optional `HYBRID_JEV_API_KEY`) or the
  open-source Laya runtime (`uv sync --extra jev`,
  `HYBRID_JEV_MODEL=auto|english|multilingual`). Long text is scanned in
  ~1400-character chunks and the maximum probability is used; a failure fails
  closed. It is **not** in `composite` and not the measured default. Offline
  result: regex 41/0/159/0; English Laya 40/0/159/1 at 0.5 on the 200-document
  synthetic corpus (41/0/159/0 at the corpus-tuned 0.4). See FINDINGS §16.7.
- `screen`: the same Jev decision through the served shape. It sends the
  System One body to `HYBRID_PII_SCREEN_URL` with `X-Demo-Step: screen:pii`,
  the `screen` API key, and the policy model name; the Praxis control-plane
  listener (`:8086`) routes it to `jev-cpu` and never to a review pool. The
  trace UI records `entry=screen`. Unset URL or any serving failure raises,
  so the caller fails closed to the sensitive review entry. It is opt-in and
  not in a measured run (FINDINGS §16.9).
- `composite`: OR of regex, Presidio and taxonomy, skipping unavailable
  backends. Jev and the served screen are deliberately excluded because they
  may load a checkpoint or call a service.

Comparison tool: `tools/compare_pii.py` (`--detectors regex,presidio`, add
`,jev` explicitly; `--cuad ... --limit 20`). `hybrid-jev-bench` sweeps the
Jev threshold offline, and `hybrid-cause-bench` measures the termination
cause trigger against the CUAD guardrail. The cause trigger is also opt-in:
`HYBRID_CAUSE_DECIDER=jev` asks the same System One client whether the
contract has termination-for-cause language, falls back to the regex on any
error, and is not adopted because it scores F1 0.625 against the recorded
0.714 on the 19-document CUAD subset (FINDINGS §16.8).

#### Span scoring: exact, containment, partial, nervaluate

`src/hybrid_demo/evalkit/spans.py` (`uv sync --extra spans`).
`extraction_scores` keeps precision/recall/f1 as containment and adds
exact/partial views plus nervaluate offset views (`by_mode`,
`offset_backend`). Offsets require document text; `build_report` loads
`data/<set>/documents/*.txt`. A prediction that cannot be located (a
paraphrase) is scored by the text modes but is a miss in nervaluate — that
is the intended reading; containment remains the headline so old reports
stay comparable.

#### Significance: statsmodels

`src/hybrid_demo/evalkit/stats.py` keeps `exact_mcnemar` as the fallback;
`tools/mcnemar.py --backend auto` records `backend: "statsmodels" | "exact"`
in the output. Table construction is `[[both, b], [c, neither]]`, exact
test, no continuity correction.

#### Ranking: ranx, NDCG, MAP

`src/hybrid_demo/retrieval/metrics.py` (`uv sync --extra ranking`).
`hybrid-retrieval-eval --metrics-backend ranx` reports `ndcg_at_10`,
`map_at_10` and `ranking_backend` alongside the historical
`recall_at_k`/`mrr`; if ranx is absent, `auto` uses the internal
implementation and says so.

#### Ingestion: docling `HybridChunker`

`src/hybrid_demo/retrieval/docling_chunking.py`
(`uv sync --extra ingest`; `--chunk-backend docling
--tokenizer-model <embedder>`). Default is `builtin`; `auto` uses docling
only when installed. Offsets are relative to the exported text
(`export_to_text()`), which is what the pipeline quotes. A provenance
charspan is used only when the slice corroborates the chunk text; otherwise
the chunk is relocated by search, or anchored with a visible `relocated`
count — never silently trust a merged chunk's first-item charspan (the
D11 bug class). `merge_peers=True` and `max_tokens=400` are defaults;
record them with the run.

#### Portfolio index: LanceDB + BM25

`src/hybrid_demo/retrieval/index.py` — `hybrid-index build/query`.
`memory` is the CI/default backend; `lancedb` when installed. Sparse search
is internal BM25 (or `bm25s` when present); hybrid score is
`alpha * dense + (1-alpha) * sparse` after min-max normalisation. Chunk
text and char offsets are stored with each vector so a hit is quotable and
checkable.

#### Semantic cache: RedisVL + Redis

`src/hybrid_demo/specialist/cache.py`
(`uv sync --extra cache`; `--cache-backend redis --cache-redis-url ...`).
The wrapper, not RedisVL, owns the policy: exact key lookup first; sensitive
context is neither stored nor served; at most one similarity candidate,
filtered to the same clause; false-positive checks compare cached vs fresh
answers. RedisVL supplies the vector index/eviction. Redis 7.4 is
RSALv2/SSPL and Redis 8 adds an AGPLv3 option; RedisVL is Apache-2.0. If
the server licence is unacceptable, Valkey is the drop-in.

#### Observability: OTel beside SQLite

`src/hybrid_demo/traceui/otel.py` — `TRACE_OTEL=jsonl` (dependency-free,
what tests use) or `TRACE_OTEL=otlp` (`uv sync --extra otel`). SQLite stays
the audit trail; export happens after the row is written and cannot break
the relay. Attributes follow `gen_ai.*` where the convention exists, plus
`app.routing.*`, `app.governance.*`, `app.hops.*`. Sensitive content is not
exported; prompt tokens come from `x-prompt-tokens` when present, otherwise
an estimate flagged `gen_ai.usage.input_tokens_estimated`. Langfuse (MIT)
is the preferred dashboard if one is published; Phoenix is ELv2, a
licensing caveat. Neither replaces the relay.

#### RAG quality: lexical screen + RAGAS

`src/hybrid_demo/evalkit/rag.py` — `evaluate`, `evaluate_records`
(deterministic, no judge), `ragas_scores` (optional, judge-dependent).
`groundedness` = fraction of answer content tokens found in evidence;
`context_precision` = fraction of evidence sentences sharing a content
token with the answer; `context_recall` = reference-token coverage.
RAGAS's numbers are only comparable across runs with the same judge model;
record the judge.

#### Simulation: llm-d-inference-sim proxy

`src/hybrid_demo/fakepool/llmd_sim.py` — run
`llm-d-inference-sim --model Semantic-Qwen --port 8001 --mode random`, then
`hybrid-pool-sim --upstream http://127.0.0.1:8001 --pool vllm-cpu --port 8000`.
Pass-through of the OpenAI surface plus `/metrics` and `/health`; streaming
relayed; adds `X-Pool`, `X-Replica`, `X-Simulator`, `X-Proxy-Busy-Ms`,
`X-Proxy-Ttft-Ms`. It does **not** invent `X-Busy-Ms`: proxy wall time is
not device time, and the cost report sums `X-Busy-Ms` — keep `hybrid-pool`
for the measured cost/corruption arms; use this adapter for transport,
streaming, latency and OpenAI-compatibility work
(`LLMD_SIM_DEVICE_TIME_HEADERS=1` is an explicit opt-in that must be
labelled). Its failure injection is HTTP/API errors, not the demo's
deterministic tiered answer corruption.

#### Batch: llm-d-batch-gateway

`src/hybrid_demo/pipeline/batch.py` — `hybrid-batch --gateway ... --requests
requests.jsonl --out results.jsonl`. Input lines are
`{"custom_id": "...", "body": {...}}` (or full `method/url/body` lines);
the client uploads `/v1/files`, creates `/v1/batches`, polls, downloads.
Point it at the **praxis ingress**, never a pool: the gateway's native path
routes to the llm-d router and would bypass the governance/audit path.
Batch is a transport for portfolio-scale submission; the Python runner
remains the measurement harness.

#### Load generation: vLLM bench / GuideLLM

`src/hybrid_demo/bench.py` — `hybrid-bench --backend vllm|guidellm
--dry-run`. The wrapper builds commands and does not parse their reports,
so their format can change without breaking us. Pin the CLI version in a
measured run and record it with the result.

#### Structured outputs

`src/hybrid_demo/pipeline/schemas.py`; `HYBRID_STRUCTURED_OUTPUTS=
vllm|openai|legacy(guided_json)`. Default is off, so request bodies and
recorded results are unchanged. Constraining decoding can change answer
length/quality; A/B it.

### 4.4 Measurement protocol for any swap

1. Pin the upstream version and record it (container tag or package
   version).
2. Run the default and the swap on the same run IDs/documents, same seed,
   same concurrency, same prompts.
3. Compare the primary metric **and** the mechanism metric: sensitive share
   for PII, chunk-offset parity for docling, NDCG/MRR for ranking, hit
   rate + FP for cache, device seconds for serving.
4. Keep the default report columns; put the new number beside them.
5. If the swap changes routing or cost, re-run the four configs; do not mix
   adapter and non-adapter rows in one headline.

### 4.5 Licensing notes

| project | licence | note |
|---|---|---|
| Presidio | MIT | spaCy model has its own licence; check the model |
| nervaluate | MIT | small, pure Python |
| statsmodels | BSD-3-Clause | |
| ranx | MIT | check its numba/numpy pins against the env |
| docling / docling-core | MIT | PDF parsing stack pulls its own deps |
| LanceDB | Apache-2.0 | embedded store |
| RedisVL | Apache-2.0 | server licence is separate (above) |
| Redis server 7.4+ | RSALv2/SSPL; Redis 8 AGPLv3 option | Valkey is BSD-3 |
| OpenTelemetry | Apache-2.0 | SDK/exporter |
| RAGAS | Apache-2.0 | judge/model terms are separate |
| llm-d-inference-sim | Apache-2.0 | |
| llm-d-batch-gateway | Apache-2.0 | |
| vLLM / GuideLLM | Apache-2.0 | |
| Langfuse | MIT (core) | check enterprise features |
| Phoenix | ELv2 | not OSI-approved; licensing caveat |

### 4.6 Verification status

- 230 tests pass; the adapter tests cover defaults, opt-in wire formats,
  offset relocation, JSONL export, mock batch transport, metric definitions
  and the cache factory. The Jev adapter test mocks the System One HTTP
  contract, the service test exercises `/v1/systemone` in fake mode, and the
  fake-stack e2e test posts `screen:pii` through the screen ingress to
  `jev-cpu`, so the suite still runs without a GPU, model download or
  network. The e2e tests drive the real routing configs against a praxis stub
  and fake pools.
- Base environment: `presidio`, `docling`, `ranx`, `redisvl`, `statsmodels`
  and the OTel SDK are **not** installed; the adapters raise explicit
  install/URL errors and the defaults still run.
- Not yet measured: Presidio on a labelled sample, Jev/Laya on real contracts
  (the only labelled corpus is synthetic), docling offset parity on the D11
  regression set, ranx NDCG/MAP on CUAD, multi-replica Redis hit rate, RAGAS
  with the demo judge, live OTLP, GPU throughput from the new wrappers.

## 5. The path back to semantic routing

**Status:** taxonomy and evaluation harness in tree; **not measured against
the contract workload and not deployed**. The pinned policies of §3 and the
B judgment-split results remain the recorded evidence until a replacement
run is measured.

### 5.1 Why a step taxonomy

The complexity taxonomy answers "how hard is this prompt?" — the right
question for open-ended chat, where the gateway sees heterogeneous clients
and cannot know what the request is for. The contract pipeline is not that
workload: the orchestrator already knows which contract step it is running,
and the four steps have different quality, latency and governance needs.
The replacement taxonomy labels the **task stage**:

| Label | What the request is doing | Default placement |
| --- | --- | --- |
| `CLASSIFY` | Identify the contract type and initial term | CPU |
| `EXTRACT` | Locate and quote a named clause | CPU |
| `RISK` | Judge one contract's exposure | GPU (`vllm-gpu` / approved) |
| `REPORT` | Aggregate the batch into a portfolio report | GPU (`vllm-gpu` / approved) |

This is stage routing, not complexity routing. Do not describe it as
complexity routing and do not compare its numbers to the complexity arm's
numbers without saying which taxonomy produced them.

The taxonomy lives in `classifiers/contract-step.json`; its anchors describe
task intent, deliberately paraphrased, none copied from the production
prompt builders.

### 5.2 Why the harness has several families

A classifier can score well on a taxonomy while learning nothing about the
task. The contract body is large and lexically stable across all four
steps, so a model that keys on "this is an MSA" can put every request in one
class. The harness (`demo/src/hybrid_demo/evalkit/contract_steps.py`, 33
cases) makes that failure visible:

- **4 real prompts** from the actual pipeline builders.
- **16 paraphrases**, four held-out wordings per label — the generalisation
  test.
- **4 same-document controls**: one contract body under all four
  instructions; the expected label follows the instruction. A body-keyed
  classifier cannot pass this family.
- **4 long-document cases**: one ~15,000-character agreement under the same
  four instructions. The filter sees only the first 4096 characters, so the
  leading instruction must still carry the label. Intentionally over
  budget; every other family must stay under it.
- **5 agent-shaped cases**: tool-result-only, assistant-only and
  system-only requests have no user turn and must fall through without a
  label; two more put the real task in the final user turn after a tool
  result or assistant turn.

The harness also checks for anchor leakage (an anchor containing a whole
test prompt, or vice versa, would make the test circular).

### 5.3 Running it

Validate the taxonomy and case set without a stack:

```bash
cd demo
uv run hybrid-eval-contract-steps --check
```

It must report `"anchor_leakage": []` and an empty
`"prompts_over_filter_budget": []` (the four long-document cases are
expected over the 4096-character budget; no other case may be). Against a
stack with the taxonomy loaded:

```bash
uv run hybrid-eval-contract-steps \
  --base-url http://127.0.0.1:8080 \
  --out runs/contract-step/eval.json
```

The runner sends the real prompt text through `/v1/chat/completions` and
reads `x-llm-d-sc-label` / `x-demo-classification` from the response — not
the model's answer, so the generation budget is capped to keep the run
cheap. `--fail-under 0.9` for CI once a threshold is agreed;
`--check --cases-out runs/contract-step/cases.jsonl` replays the exact
cases through another tool.

### 5.4 What counts as a pass

All of the following, not just a high overall accuracy:

1. `same_document_control` is 4/4 — the non-negotiable control.
2. `long_document_control` is also 4/4 — a taxonomy that only works on
   short prompts cannot route this pipeline.
3. `agent_no_user` is 3/3 — no invented label without a user turn; the two
   agent cases with a final user turn are classified from that turn.
4. Every label has non-zero recall — a taxonomy that never predicts
   `REPORT` is not a four-way router.
5. Real-prompt and paraphrase families are both strong — a large gap means
   the classifier is matching template wording, not the task.
6. No anchor leakage and no **unexpected** truncation.
7. The score is from this taxonomy — a run against a complexity-backed
   llm-d-sc instance is not evidence for contract-step routing.

Report the confusion matrix and per-family counts with the accuracy; a
single number hides exactly the failure the controls exist to catch.

### 5.5 Deploying it after it passes

The deployment is intentionally not switched, and note that llm-d-sc is no
longer in the base stack (§3.2) — re-adding it is the first step:

1. Restore an llm-d-sc Deployment + Service (git history:
   `manifests/base/llm-d-sc.yaml` at commit `aa080e9`) and mount
   `classifiers/contract-step.json` into the pod (a ConfigMap volume is
   enough; the taxonomy is data, not new weights).
2. Set `LLM_D_SC_CLASSIFIER` to the mounted path instead of `complexity`.
3. Set the filter's `signal: contract_step` to match the taxonomy's
   `signal` — a mismatch is rejected as `INVALID_ARGUMENT`.
4. Replace the pinned routes with `CLASSIFY`/`EXTRACT`/`RISK`/`REPORT`
   routes and keep a `default_cluster` for fail-open behaviour.
5. Keep the PII screen and the approved-pool rule unchanged. Sensitivity
   beats stage: a sensitive `RISK` or `REPORT` still uses an approved entry,
   and a sensitive document must never fall back to an unapproved pool
   because the stage route selected one.

The classifier instance serves one signal — if both complexity and
contract-step are needed, run two instances, not one multi-signal instance.
The gateway is `quay.io/mtahhan/gci-praxis:v0.9.1` (Praxis 0.7.5 /
ai-gateway 0.9.0, PPE 0.4.1 host plugins), a custom build carrying the
out-of-tree `llm_d_sc` filter and the PPE policy host that upstream `ai`
does not have. The filter is unused by the default configs but stays in the
image for this experiment; the policy host is used by every entry. The
rebuild path is `deploy/images/praxis-llm-d-sc/`.

### 5.6 Claim boundary

Do not claim a measured contract-step result until the harness has been run
against this taxonomy and the same-document control passes. The earlier B
judgment-split result and the D all-CPU result were measured on the
complexity taxonomy (or without the classifier deciding the stage); they
are not contract-step results.

## 6. Reproducibility

Published images (all from the `publish-images` workflow on amd64 Actions
runners because the dev machines are arm64):

| Image | Published | Digest | Consumed by the default stack |
| --- | --- | --- | --- |
| `quay.io/mtahhan/gci-praxis:v0.9.1` | 2026-11-06, run `42202721911` | `sha256:9def8e14befedeef75a088266630386dc4506004dd43fd6962212dcb122f375a` | **yes** — the gateway, plus the PPE policy host (audit observer + shadow PII scanner) |
| `quay.io/mtahhan/gci-praxis:v0.9.0` | 2026-11-05, run `42043729532` | `sha256:2187ab818efab9a8f3d1f911190d47fad960888bada7d3db8a973d3ad70ac992` | superseded by v0.9.1 |
| `quay.io/mtahhan/gci-llm-d-sc:main-e660fff` | 2026-11-05 | `sha256:d94c1f7226bfbaeb5a300f0efdbab0e8a8988fc16af7fd80cff0e008a11fa3e1` | no — §5 experiment only |
| `quay.io/mtahhan/gci-llm-d-sc-modelcar:complexity` | 2026-11-05 | `sha256:299874af83c565d8435c8e061b87da1754cdd918ca15e977a7fecbf3aa17f491` (mirror, byte-identical) | no — §5 experiment only |

The digests are the reproducibility boundary for measured runs; record the
digest, not the tag. The base manifests and the Linux compose stack point at
`v0.9.1` (the opt-in praxis-0.9.0 overlay is retired — base is the 0.9.1
stack), and `oc apply -k manifests` on a live cluster lands the image switch
together with the classifier removal and the PPE policy documents. Treat the
rebuilt stack as new and re-run the measurement arms (FINDINGS §12). What is
real vs simulated per environment is tabulated in README ("What is measured
vs simulated"); quote test-split numbers only (FINDINGS §3, §13).

## 7. Design history

Older records: the decision log through 2026-11-05 morning is `DECISIONS.md`
at commit `8798da3`; the full dated analyses it folded are
`docs/handoff-2026-11-04-{rosa-routing,cpu-model-recipes}.md` at the same
commit. The durable content of the last decisions lives in FINDINGS: the
routing-claims scoping and its surviving warnings in §15.5/§15.6, the
CPU-model test plan in §9.

- **2026-11-04 — config names become policies.** Renamed the ROSA run
  directories `arm-A`–`arm-D` to `all-gpu`/`gpu-small`/`round-robin`/
  `hybrid`; added `hybrid-run --target direct|gateway` and `--config`
  (with `--arm` hidden but working); recorded `summary.config` alongside
  the legacy `summary.arm`; rebuilt the pipeline SVGs and updated scripts,
  the guide and the run READMEs to policy names.
- **2026-11-05, morning — one config catalog; UI hardening.**
  `demo/src/hybrid_demo/configs.json` became the single source of config
  metadata (loaded by Python, the UI payload and `scripts/configs.sh`);
  the dashboard shows recorded pool hardware from `meta.pool_hardware`;
  report/trace labels say config. The stage UI's live mode gained
  token-gated auth (`--live-token`), archive size caps (413 on oversize),
  and a bounded FIFO document store; the Makefile wraps the three start
  modes (`dashboard-up`, `dashboard-live`, `dashboard-live HOST=... LIVE_TOKEN=...`).
- **2026-11-05, afternoon — published images; the classifier leaves the
  request path.** The `publish-images` workflow published the gateway,
  classifier and mirrored modelcar (digests in §6); commit `eacc019`
  switched the base manifests and Linux compose to them and retired the
  praxis-0.9.0 overlay. `CorpusError` started carrying its status code
  (413 for size caps, 400 for unreadable/binary/empty) so reworded
  refusals cannot degrade to a 400. And the classifier was removed from
  the default stack (§3.2): llm-d-sc dropped from manifests and compose,
  `config.yaml` rewritten as pinned CPU-first routing, `arm-b.yaml`
  rewritten as the gpu-small pin (its label-routed predecessor and the
  llm-d-sc Deployment are in git history at `aa080e9`), the praxis stub
  parses pin+failover chains without inventing labels, the e2e fallback
  tests assert the new contract, and the publish workflow lost the
  classifier/modelcar jobs. Built and tested — **not measured**; every
  §15 number before §15.8 came from the classifier-in-path config. This
  document, the DECISIONS.md deletion and the FINDINGS transfers landed with
  it.
- **2026-11-06 — PPE policy layer in the gateway image.** Image `v0.9.1`
  host-registers the reference `audit/logger` (observer mode on PPE 0.4.1)
  and `validator/gci-pii-observe`, an observe-only PII scanner whose pattern
  catalog mirrors `pii.py`. Every entry gained a per-step `X-API-Key` identity
  gate (fail closed); the sensitive entries taint the document session with a
  gateway-owned `sensitive` label. The layer attributes and audits; it does
  not steer pools (§3.1). The gateway image and its validation job are in
  `.github/workflows/`, the build in `deploy/images/praxis-llm-d-sc/`.
- **2026-11-09 — the classifier-free policy is measured, twice.** The
  200-document ROSA matrix was re-run without the classifier and with the
  conditional CPU termination follow-up (§3.3). The first measurement scored
  198/200 with risk 0.990; the quoted rerun on a rebuilt stack scored
  200/200, risk 1.000, 816.59 s and 0.22 GPU-s/doc. The two risk flips did
  not reproduce — temperature-0 serving variance, not a routing regression
  (FINDINGS §15.9). The GPU-termination alternative is measured but is not
  the policy. Historical runs from the classifier-in-path and earlier
  stacks are archived at commit `cfc52cc`; the working tree keeps the quoted
  record and the guide evidence (`runs/README.md`).

## 8. Open decisions and next actions

### 8.1 Open: rename `all-gpu` and `gpu-small`?

The names still imply hardware. A behavior-based set would be harder to
misread:

| Current | Proposed | Why |
|---|---|---|
| `all-gpu` | `one-pool` or `single-route` | all requests to one through-stack route; no hardware claim |
| `gpu-small` | `small-first` | per-document work to the small model |
| `round-robin` | `round-robin` | already behavioral |
| `hybrid` | `hybrid` (or `cpu-first`) | already established; optionally make the behavior explicit |

Recommendation: rename at least A and B to behavior names, keep every old
name and letter as an alias, keep the route/cluster names unchanged for
evidence compatibility, and do not rename historical run directories. If
not renaming, the minimum alternative is a hardware badge everywhere the
config name appears. (Done-and-closed items from the same list: the config
metadata single source of truth, visible run hardware, config-labelled
reports, the regenerated ROSA report, and the alias regression tests —
§2.7 and §7 record them.)

### 8.2 Next actions, in order

1. **Rebuild ROSA** from `deploy/openshift/rosa/README.md`: g5 capacity
   probe, both machine pools in one AZ, GPU operator,
   `scripts/cluster/cpu-topology.sh` bind lists into `pools-cpu.yaml`,
   storage + seeders, `oc apply -k deploy/openshift/rosa`, specialist
   image rebuild, then the 2-document smoke run (1 sensitive + 1
   non-sensitive, config hybrid).
2. **The 200-document matrix is done (2026-11-10); direct baselines are from
   2026-11-09.** The quoted through-stack record is §3.3 / FINDINGS §15.10
   (`runs/rosa-200-2026-11-10-praxis-routes`). Re-run
   (`DOCS=200 scripts/cluster/arms.sh` plus the direct baselines) only after a
   routing, model or pool change; give it a new run directory and do not
   overwrite the quoted one.
3. **Contract-step taxonomy eval** once a stack is up
   (`hybrid-eval-contract-steps`; pass = same-document control 4/4, §5),
   then the CUAD test-split specialist through-stack rerun at lower
   concurrency (FINDINGS §9). `--check` runs locally without a stack.
4. **Local, no cluster needed:** the Presidio A/B on a labelled sample and
   the docling-offsets A/B on the D11 regression set (§4.6's open list),
   and the embedder-tier call (FINDINGS §9/§11).
5. **MCP facade and agent trigger** (§3.5, no re-measurement required). The
   facade is scaffolded (`hybrid-mcp`, `src/hybrid_demo/mcp/server.py`, root
   launcher `mcp/contract_review_server.py`) with `contract://`, the full
   review and the stage tools. Remaining: wire OpenClaw to it, add
   `search_clauses` once the index lands, and run the same two-document batch
   scripted and agent-driven. Keep the agent path labelled **functional, not
   measured** until it has its own run record; the measured reference path is
   unchanged.
6. **Decision-model experiment (Jev/Laya-style): implemented, opt-in, not
   adopted.** The best fit is the PII screen (no CUAD PII labels, which is
   exactly the zero-shot case), with the cause-language trigger as a second
   candidate. `pii.py` has `jev` (local Laya or `HYBRID_JEV_URL`), `screen`
   (the served `screen:pii` control-plane route), and the specialist has
   `HYBRID_CAUSE_DECIDER=jev`. `hybrid-jev-bench` and `hybrid-cause-bench`
   record the offline results; the regex screen and regex trigger remain the
   measured defaults and no run directory exists for Jev. On the 200-document
   synthetic corpus the English Laya checkpoint is 40/0/159/1 at 0.5 and
   41/0/159/0 at 0.4; regex is 41/0/159/0, so this is a no-label option, not
   a measured improvement (FINDINGS §16.7). The cause trigger is worse on the
   CUAD guardrail (F1 0.625 vs 0.714) and is rejected (§16.8). The served
   `jev-cpu` pool/relay and screen listener are implemented and exercised on
   the fake stack, but the cluster has not been rebuilt (§16.9). Not a fit
   for stage routing (already measured and removed) or wholesale risk
   judgment.

Quick verification of the whole surface:

```bash
cd demo && uv run pytest -q
uv run hybrid-run --target gateway --help   # shows --config, not --arm
cd .. && for f in scripts/configs.sh scripts/linux/set_arm.sh \
         scripts/linux/set_config.sh scripts/linux/arms.sh \
         scripts/linux/demo.sh scripts/cluster/arms.sh \
         scripts/cluster/model-comparison.sh; do bash -n "$f" || exit 1; done
xmllint --noout docs/pipeline-direct.svg docs/pipeline-through-stack.svg \
                 docs/architecture-as-built.svg
```
