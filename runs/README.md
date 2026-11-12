# Run directory map

For the route/model/hardware distinction, see
[`../docs/design.md`](../docs/design.md).

Only the current record, the previous measured record, and the evidence used
by the stage guide are kept in the working tree.

## Current and previous records

| directory | routing config | what it is |
|---|---|---|
| `rosa-200-2026-11-12-praxis-routes/hybrid` | `hybrid` | **current quoted record** — 2026-11-12 RC1, real CPU + GPU pools, 200 docs, 0 violations, 0 errors |
| `rosa-200-2026-11-12-praxis-routes/one-pool` | `one-pool` | same-run control; general-entry PII gate denied 246 sensitive steps (not a quality comparison) |
| `rosa-200-2026-11-12-praxis-routes/small-first` | `small-first` | same-run control; same PII-gate boundary |
| `rosa-200-2026-11-12-praxis-routes/round-robin` | `round-robin` | same-run control; same PII-gate boundary |
| `rosa-200-2026-11-11-praxis-routes/hybrid` | `hybrid` | previous measured record — 2026-11-11 `v0.9.1`, last comparable four-arm matrix |
| `rosa-200-2026-11-11-praxis-routes/one-pool` | `one-pool` | previous same-run control; 247 sensitive-pool violations |
| `rosa-200-2026-11-11-praxis-routes/small-first` | `small-first` | previous same-run control; 247 violations |
| `rosa-200-2026-11-11-praxis-routes/round-robin` | `round-robin` | previous same-run control; 110 violations |
| `rosa-200-2026-11-11-direct/cpu-moe` | direct | CPU MoE quality baseline |
| `rosa-200-2026-11-11-direct/gpu-moe` | direct | GPU MoE quality baseline |
| `rosa-200-2026-11-11-direct/cpu-small` | direct | 1.5B CPU quality baseline |
| `rosa-200-2026-11-10-praxis-routes/hybrid` | `hybrid` | earlier measured record — rebuilt two-entry / `X-Demo-Step` graph, 200 docs |
| `rosa-200-2026-11-10-praxis-routes/all-gpu` | `all-gpu` | earlier same-run one-pool control (run dir keeps the historical label; 11 transport timeouts) |
| `rosa-200-2026-11-10-praxis-routes/gpu-small` | `gpu-small` | earlier same-run `small-first` matrix row |
| `rosa-200-2026-11-10-praxis-routes/round-robin` | `round-robin` | earlier same-run matrix row |
| `cuad-test/` | direct + gateway | CUAD example and appendix used by the guide |
| `retrieval/` | local direct | specialist quality evidence |

The 2026-11-12 matrix is the current quoted record. It is the first
200-document row on the real ROSA pools with the RC1 `v0.9.2` image. The
hybrid arm kept all 247 sensitive steps on approved pools, with 0 violations
and 0 errors, and measured **0.259 GPU-s/doc** plus **33.006 CPU core-s/doc**.
Extraction F1 was 1.000, risk 0.995, report 0.998, and 199/200 documents were
fully correct; wall time was 818.87 s.

The three RC1 controls deliberately send every document through the general
entry. The new PII gate denied 246 sensitive steps in each control and one
content miss per run reached a non-approved route. Their quality rows cover
only the 159 non-sensitive documents and are **not comparable** to the
2026-11-11 controls. The 2026-11-11 `v0.9.1` matrix remains the last
comparable four-arm routing measurement: hybrid 806.59 s, 0.265 GPU-s/doc and
32.555 CPU core-s/doc; one-pool and small-first 247 expected violations,
round-robin 110. The 2026-11-10 matrix is the earlier measured record. Do not
edit or reinterpret a recorded run; create a new directory for the next
measurement.

## Real-pool smoke and policy evidence

| directory | stack | what it is |
|---|---|---|
| `rosa-resilience-2026-11-12/` | ROSA real pools, `v0.9.2` | full `STACK=rosa` smoke: **28/28** checks; CPU fallback/recovery, shared-GPU fail-closed/recovery, RC1 gate; not a quality row |
| `rosa-smoke-2026-11-12/` | ROSA real pools, `v0.9.2` | two-document governed smoke: 2/2 correct, 7/7 sensitive approved, 0 violations |
| `rosa-policy-2026-11-12-real/` | ROSA real pools, `v0.9.2` | live gate assertions: keyless/unknown denied, wrong-step denied, PII 403, sensitive entry allowed, audit parsed |
| `rosa-policy-2026-11-12/` | ROSA `fake-all`, `v0.9.2` | 27/27 functional policy smoke; **not** a model-quality row |

No number from a smoke or policy run belongs in a measured row. The real-pool
resilience suite and gate assertions are the RC1 live operational evidence;
the 2026-11-12 matrix is the live quality record. On the real stack the
general and approved GPU Services select the same `gpumoe-vllm` Deployment,
so the resilience test asserts sensitive fail-closed when that shared
backend is down; it does not claim an isolated approved-only outage.

## Removed historical runs

These directories were pruned from the working tree to keep the checkout
focused on the current record. They are still recoverable from git history at
commit `235311b` (the commit immediately before the earlier cleanup):

- `runs/rosa-200/`
- `runs/direct/`
- `runs/simulated/`
- `runs/gpu-measured/`
- `runs/cuad-mixed/`
- `runs/cuad-test-hybrid-2026-11-09/`
- `runs/rosa-direct-vs-stacked/`
- `runs/rosa-200-2026-11-09-cpu-term/` (first measurement of the same policy)
- `runs/rosa-200-2026-11-09-gpu-term/` (measured GPU-termination alternative)
- `runs/live-2026-11-09/`
- `runs/flightpath-functional/`

The pre-rebuild 2026-11-09 matrix and its rerun were pruned in the 2026-11-11
cleanup; they are recoverable at commit `a0fa8ec`:

- `runs/rosa-200-2026-11-09/`
- `runs/rosa-200-2026-11-09-cpu-term-rerun/`

Recover one directory with:

```bash
git checkout a0fa8ec -- runs/rosa-200-2026-11-09
git checkout 235311b -- runs/rosa-200-2026-11-09-gpu-term
```
