# 2026-11-12 RC1 real-pool ROSA record

Real ROSA CPU + GPU pools, image `quay.io/mtahhan/gci-praxis:v0.9.2`
(`sha256:3140a8dad2626ef71504546fa7901f53d9ec68ac49ed20e39919ae0af71c77c6`),
RC1 step-key binding and general-entry PII gate enabled.

## Provenance

| field | value |
|---|---|
| cluster | `<redacted-cluster>` (ROSA HCP 4.22.6, `us-east-1a`) |
| CPU node | `<redacted-node-5>`, `x8i.24xlarge` (48 physical / 96 logical, 2 NUMA nodes) |
| GPU node | `<redacted-node-6>`, `g5.12xlarge`, 4× A10G |
| gateway | `v0.9.2`, digest above |
| dataset | 200 synthetic documents, seed 42, 41 sensitive |
| screen | regex, 41 tp / 0 fp / 159 tn / 0 fn |
| concurrency | 12 |
| run start | `2026-11-12T13:19:51Z` (hybrid arm) |
| git | `1cd6733`, worktree dirty (same three one-line uncommitted edits as the 2026-11-11 record) |

The CPU topology script reproduced the recorded layout exactly (`problems: []`):

- `cpureal-vllm-cpu` `0-15` (NUMA 0)
- `cpureal-specialist` `16-17` (NUMA 0)
- `cpureal-vllm-gpu-small` `24-35` (NUMA 1)

`/proc/1/status` inside each live pod reported those same `Cpus_allowed_list`
values. See `evidence/pinning-and-gpu.txt` and `evidence/cpu-topology.txt`.

## Hybrid — current quoted RC1 record

| metric | value |
|---|---:|
| documents | 200 |
| wall | **818.87 s** |
| steps | 1,201 |
| errors | **0** |
| fallbacks | **0** |
| extract F1 | **1.000** |
| risk accuracy | **0.995** |
| report accuracy | **0.998** |
| docs fully correct | **199/200** |
| sensitive steps on approved pools | **247/247** |
| sensitive violations | **0** |
| GPU-s/doc | **0.259** |
| CPU core-s/doc | **33.006** |
| judgment on GPU | 201/201 |
| CPU-served share | 83.3% |

Routes: `vllm-cpu` 400, `cuad-specialist` 600, `vllm-gpu` 159,
`vllm-gpu-approved` 42. Entries: general 954, sensitive 247.

The 2026-11-11 `v0.9.1` hybrid row remains the previous measured record:
806.59 s, F1/risk/report 1.000, 200/200, 0 violations, 0.265 GPU-s/doc and
32.555 CPU core-s/doc.

## Controls — PII gate boundary, not quality comparisons

The `one-pool`, `small-first`, and `round-robin` configs have no sensitive
listener: they send every document through the general entry. Under RC1 the
general entry now denies PII, so those arms are blocked before the pool:

| config | wall s | non-sensitive quality | sensitive steps approved | errors | sensitive violations |
|---|---:|---|---:|---:|---:|
| one-pool | 157.03 | F1 0.887, risk 0.795, report 0.636; 159/200 fully correct | 0/247 | 246 | 1 |
| small-first | 377.68 | F1 0.887, risk 0.345, report 0.318; 67/200 fully correct | 0/247 | 246 | 1 |
| round-robin | 725.35 | F1 0.887, risk 0.795, report 0.636; 159/200 fully correct | 0/247 | 246 | 1 |

Each control has 246 deny records and one sensitive step that slipped through
on a non-approved route because that request's content did not match the PII
pattern. This is the reason the hybrid must choose the sensitive entry: the
content gate is not a substitute for the sensitive-entry boundary.

Do **not** compare these quality rows with the 2026-11-11 controls. The
2026-11-11 `v0.9.1` matrix is the last comparable four-arm routing
measurement (one-pool and small-first 247 violations, round-robin 110).

## Files

- `hybrid/summary.json`, `hybrid/results.json`, `hybrid/metrics.json`
- `report.md`, `report.json`, `report.html`
- `evidence/` — topology, live CPU pinning, GPU allocatable count
