# Run directory map

For the route/model/hardware distinction, see
[`../docs/design.md`](../docs/design.md).

Only the current record and the evidence used by the stage guide are kept in
the working tree.

## Kept

| directory | routing config | what it is |
|---|---|---|
| `rosa-200-2026-11-10-praxis-routes/hybrid` | `hybrid` | **current quoted record** — rebuilt two-entry / `X-Demo-Step` graph, 200 docs, default dashboard A |
| `rosa-200-2026-11-10-praxis-routes/all-gpu` | `all-gpu` | same-run all-GPU control, default dashboard B (11 transport timeouts) |
| `rosa-200-2026-11-10-praxis-routes/gpu-small` | `gpu-small` | same-run matrix row |
| `rosa-200-2026-11-10-praxis-routes/round-robin` | `round-robin` | same-run matrix row |
| `rosa-200-2026-11-09-cpu-term-rerun/hybrid` | `hybrid` | pre-rebuild record — four-entry policy with composite `vllm-cpu`; keep for comparison |
| `rosa-200-2026-11-09/all-gpu` | `all-gpu` | pre-rebuild all-GPU control |
| `rosa-200-2026-11-09/gpu-small` | `gpu-small` | pre-rebuild matrix row |
| `rosa-200-2026-11-09/round-robin` | `round-robin` | pre-rebuild matrix row |
| `cuad-test/` | direct + gateway | CUAD example and appendix used by the guide |
| `retrieval/` | local direct | specialist quality evidence |

## 2026-11-11 verification runs

Produced after the OpenShell sandbox fix (`feea584`) on the rebuilt ROSA
stack. They are verification evidence, not a replacement for the quoted
`2026-11-10` record.

| directory | routing config | what it is |
|---|---|---|
| `rosa-200-2026-11-11-praxis-routes/hybrid` | `hybrid` | governed 200-doc arm: 0 violations, 0 errors |
| `rosa-200-2026-11-11-praxis-routes/one-pool` | `one-pool` | same-run control; sensitive-pool violations expected |
| `rosa-200-2026-11-11-praxis-routes/small-first` | `small-first` | same-run control; sensitive-pool violations expected |
| `rosa-200-2026-11-11-praxis-routes/round-robin` | `round-robin` | same-run control |
| `rosa-200-2026-11-11-direct/cpu-moe` | direct | CPU MoE quality baseline |
| `rosa-200-2026-11-11-direct/gpu-moe` | direct | GPU MoE quality baseline |
| `rosa-200-2026-11-11-direct/cpu-small` | direct | 1.5B CPU quality baseline |

The `2026-11-09` runs measured the old four-entry policy with a composite
`vllm-cpu` route (CUAD extraction plus forwarding). Their quality, route
counts and cost stay tied to that graph. The `2026-11-10-praxis-routes` run is
the rebuilt two-entry / `X-Demo-Step` graph: `cuad-specialist` is a separate
route and no app-side forwarder exists. Do not edit or reinterpret a recorded
run; create a new directory for the next measurement.

## Removed historical runs

These directories were pruned from the working tree to keep the checkout
focused on the current record. They are still recoverable from git history at
commit `cfc52cc` (the commit immediately before the cleanup):

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

Recover one directory with:

```bash
git checkout cfc52cc -- runs/rosa-200-2026-11-09-gpu-term
```
