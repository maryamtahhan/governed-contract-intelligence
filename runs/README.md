# Run directory map

For the route/model/hardware distinction, see
[`../docs/design.md`](../docs/design.md).

Only the current record, the previous measured record, and the evidence used
by the stage guide are kept in the working tree.

## Current and previous records

| directory | routing config | what it is |
|---|---|---|
| `rosa-200-2026-11-11-praxis-routes/hybrid` | `hybrid` | **current quoted record** — 2026-11-11 re-run, 200 docs, default dashboard A, 0 violations, 0 errors |
| `rosa-200-2026-11-11-praxis-routes/one-pool` | `one-pool` | same-run control, default dashboard B; 247 sensitive-pool violations expected |
| `rosa-200-2026-11-11-praxis-routes/small-first` | `small-first` | same-run control; 247 violations expected |
| `rosa-200-2026-11-11-praxis-routes/round-robin` | `round-robin` | same-run control; 110 violations |
| `rosa-200-2026-11-11-direct/cpu-moe` | direct | CPU MoE quality baseline |
| `rosa-200-2026-11-11-direct/gpu-moe` | direct | GPU MoE quality baseline |
| `rosa-200-2026-11-11-direct/cpu-small` | direct | 1.5B CPU quality baseline |
| `rosa-200-2026-11-10-praxis-routes/hybrid` | `hybrid` | previous measured record — rebuilt two-entry / `X-Demo-Step` graph, 200 docs |
| `rosa-200-2026-11-10-praxis-routes/all-gpu` | `all-gpu` | previous same-run one-pool control (run dir keeps the historical label; 11 transport timeouts) |
| `rosa-200-2026-11-10-praxis-routes/gpu-small` | `gpu-small` | previous same-run `small-first` matrix row |
| `rosa-200-2026-11-10-praxis-routes/round-robin` | `round-robin` | previous same-run matrix row |
| `cuad-test/` | direct + gateway | CUAD example and appendix used by the guide |
| `retrieval/` | local direct | specialist quality evidence |

The 2026-11-11 matrix is the current quoted record. It re-ran the rebuilt
two-entry graph after the OpenShell sandbox egress fix: the hybrid arm kept
all 247 sensitive steps on approved pools, with 0 violations and 0 errors,
and measured **0.265 GPU-s/doc** (plus 32.555 CPU core-s/doc). The report
rollup is deterministic from the per-document rows, so the recorded report
accuracy is 1.000 rather than the previous 0.750.

The 2026-11-10 matrix remains the previous measured record. Its hybrid arm
scored extract F1 1.000, risk 1.000 and report 0.750; its one-pool control
had 11 transport timeouts. Do not edit or reinterpret a recorded run; create a
new directory for the next measurement.

## Removed historical runs

These directories were pruned from the working tree to keep the checkout
focused on the current record. They are still recoverable from git history at
commit `cfc52cc` (the commit immediately before the earlier cleanup):

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
cleanup; they are recoverable at commit `25bfa4d`:

- `runs/rosa-200-2026-11-09/`
- `runs/rosa-200-2026-11-09-cpu-term-rerun/`

Recover one directory with:

```bash
git checkout 25bfa4d -- runs/rosa-200-2026-11-09
git checkout cfc52cc -- runs/rosa-200-2026-11-09-gpu-term
```
