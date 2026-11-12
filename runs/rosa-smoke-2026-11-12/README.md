# 2026-11-12 real-pool two-document smoke

First live run after bringing up the real ROSA `x8i.24xlarge` and
`g5.12xlarge` pools. Arm D (`hybrid`), two synthetic documents selected
explicitly: `C-2026-0001` (non-sensitive) and `C-2026-0003` (sensitive).

| metric | value |
|---|---:|
| wall | 19.17 s |
| steps | 13 |
| errors | 0 |
| fallbacks | 0 |
| extract F1 / risk / report | 1.000 / 1.000 / 1.000 |
| docs fully correct | 2/2 |
| sensitive steps on approved pools | 7/7 |
| sensitive violations | 0 |
| entries | general 6, sensitive 7 |

Routes: `vllm-cpu` 4, `cuad-specialist` 6, `vllm-gpu-approved` 2,
`vllm-gpu` 1. Screen: 1 tp, 0 fp, 1 tn, 0 fn.

Command:

```bash
RUNS=runs/rosa-smoke-2026-11-12 \
  DOC_IDS=C-2026-0001,C-2026-0003 ARMS=D DOCS=2 CONCURRENCY=2 \
  CLUSTER=<redacted-cluster> scripts/cluster/arms.sh
```

Provenance: ROSA `<redacted-cluster>`, `v0.9.2`
(`sha256:3140a8dad2626ef71504546fa7901f53d9ec68ac49ed20e39919ae0af71c77c6`),
CPU `x8i.24xlarge`, GPU `g5.12xlarge` (4× A10G), both in `us-east-1a`.
