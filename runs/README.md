# Run directory map

For the route/model/hardware distinction and the open naming decisions, see
[`../HANDOFF.md`](../HANDOFF.md).

## `runs/rosa-200/` — through-stack, 200 synthetic documents

These are **governed** runs: the batch runner went through the trace-ui relays
and praxis. They are configs, not hardware claims.

| directory | routing config | what it does |
|---|---|---|
| `all-gpu/` | `all-gpu` | every request to the GPU pool |
| `gpu-small/` | `gpu-small` | simple work to the small GPU pool |
| `round-robin/` | `round-robin` | no classifier; rotate across pools |
| `hybrid/` | `hybrid` | classifier picks CPU or GPU; sensitive work stays on approved pools |

The old names were `arm-A` through `arm-D`; the letters remain internal keys in
`summary.json`, but the directories now use the policy name.

## `runs/direct/` — direct, no gateway

These are **ungoverned** runs: the batch runner talked to one bare vLLM
endpoint. There is no relay, praxis, label, pool choice or trace.

| directory | target |
|---|---|
| `gpu-moe/` | one GPU endpoint (4× A10G), all stages |
| `cpu-moe/` | one CPU endpoint, all stages |
| `cpu-small/` | one small-model CPU endpoint, all stages |

## Historical groups — keep the recorded names

`runs/simulated/`, `runs/gpu-measured/`, `runs/cuad-mixed/`,
`runs/cuad-test/` and the root `runs/arm-D/` are earlier evidence. Their
directory names are preserved as recorded run ids. `arm-D-*` means the old
internal key D, now called **`hybrid`**; `arm-direct-*` means a direct run.
`runs/arm-D/` itself is a 2-document smoke run, not the 200-document measured
run. The current CLI and new directories use policy names.

If you remember one thing: **`rosa-200/hybrid` is through-stack; `direct/gpu-moe`
is direct GPU.**
