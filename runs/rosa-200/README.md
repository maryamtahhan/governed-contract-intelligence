# ROSA 200-document runs

All four directories are **through-stack** runs on the measured ROSA cluster.
The directory name is the routing policy, not the hardware.

| directory | config | meaning |
|---|---|---|
| `all-gpu/` | `all-gpu` | every request to the GPU pool |
| `gpu-small/` | `gpu-small` | simple work to the small GPU pool |
| `round-robin/` | `round-robin` | no classifier; rotate across pools |
| `hybrid/` | `hybrid` | CPU-first: single-document work stays on CPU; sensitive work stays approved |

`hybrid/` was previously `arm-D/`. It is **not** the direct GPU run; that one is
`runs/direct/gpu-moe/`. It is also **not** demonstrated complexity routing:
llm-d-sc labelled 1,200 of 1,201 steps MEDIUM, and the specialist forwarded the
non-extraction calls to the CPU MoE. The GPU served only the cross-document
report. See `docs/handoff-2026-11-04-rosa-routing.md`.

See `../README.md` for the full run map.
