# Run directory map

For the route/model/hardware distinction, see
[`../docs/design.md`](../docs/design.md).

Only the current record and the evidence used by the stage guide are kept in
the working tree.

## Kept

| directory | routing config | what it is |
|---|---|---|
| `rosa-200-2026-11-09-cpu-term-rerun/hybrid` | `hybrid` | **current quoted record** — CPU termination policy, 200 docs, default dashboard A |
| `rosa-200-2026-11-09/all-gpu` | `all-gpu` | same-day all-GPU control, default dashboard B |
| `rosa-200-2026-11-09/gpu-small` | `gpu-small` | same-day matrix row |
| `rosa-200-2026-11-09/round-robin` | `round-robin` | same-day matrix row |
| `cuad-test/` | direct + gateway | CUAD example and appendix used by the guide |
| `retrieval/` | local direct | specialist quality evidence |

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
