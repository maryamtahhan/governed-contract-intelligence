# Real ROSA RC1 resilience smoke — 2026-11-12

Full assertion suite from `scripts/cluster/smoke.sh`, run with
`STACK=rosa` against the recreated real CPU and GPU pools. This is
functional routing, fail-closed, recovery, and policy evidence. It is **not**
a 200-document or model-quality record; the quoted quality row remains
`runs/rosa-200-2026-11-12-praxis-routes/hybrid`.

| item | value |
|---|---|
| cluster | ROSA `<redacted-cluster>`, `us-east-1a` |
| image | `quay.io/mtahhan/gci-praxis:v0.9.2` |
| digest | `sha256:3140a8dad2626ef71504546fa7901f53d9ec68ac49ed20e39919ae0af71c77c6` |
| CPU node | `<redacted-node-2>` (`x8i.24xlarge`, 48 physical / 96 logical CPUs, 2 NUMA nodes, AMX) |
| GPU node | `<redacted-node-1>` (`g5.12xlarge`, 4× A10G, allocatable `nvidia.com/gpu=4`) |
| checks | **28/28 passed** |
| policy request records | 20 |
| scanner lines | 6 observe + 15 gate |
| audit traces | 22 JSONL records |
| sensitive-entry traces | 7, all on approved pools |

Command:

```bash
STACK=rosa scripts/cluster/smoke.sh
```

The script carried all requests through the trace-ui general/sensitive
ingress and the five pool relays. It did not call the model pods directly.

## Resilience assertions

**CPU pool loss (section 8–9).** Scaling `cpureal-vllm-cpu` to zero made
general classify degrade to `vllm-gpu`; the trace recorded `fallback=1` and a
failed CPU hop. General risk stayed on `vllm-gpu`. After restarting the CPU
pool, classify returned to `vllm-cpu` with `fallback=0` and no failed hop.

**Shared GPU backend loss (section 10).** On this stack `vllm-gpu` and
`vllm-gpu-approved` are two Services selecting the same `gpumoe-vllm`
Deployment, so an approved-only pool outage cannot be isolated. Scaling that
one Deployment to zero produced:

- sensitive risk: **502**, never served on another pool;
- general risk: **502** (its configured failover retries the same GPU route);
- sensitive classify: **200** on `vllm-cpu`, unaffected;
- after restart: sensitive risk **200** on `vllm-gpu-approved`.

The fake-all smoke can isolate the approved fake deployment; the real stack
cannot, and this record states that limitation rather than inventing an
isolation claim.

## Policy assertions

- keyless general request: `401`;
- unknown key: `401`;
- classify key claiming `X-Demo-Step: risk`: `403`;
- PII-positive general request: `403`;
- clean general request after the denial: `200`;
- sensitive-entry PII request: `200`;
- step-less eval identity: `200`;
- praxis emitted 20 `gci-gateway` records, 6 `gci_pii_observe` lines and
  15 `gci_pii_gate` lines; the gate recorded the denial and the scanner
  observed PII content on the sensitive prompts.

## Topology and first attempt

`cpu-topology.txt` records the node proposal: MoE `0-15` on NUMA 0,
specialist `16-17` on NUMA 0, 1.5B `24-35` on NUMA 1, `problems: []`.

The first run (`smoke-attempt-1.log`) stopped at section 3 because the test
sent a generic “extract the termination clause” prompt. The real CUAD
specialist correctly rejects prompts without a contract body and the
pipeline's `Return JSON` suffix (`400 unsupported_prompt`); the fake
specialist is more permissive. The test was corrected to send the real
contract shape, a direct probe returned `200` with the extracted clause, and
the rerun passed all 28 checks. That was a test-shape bug, not a
specialist or routing failure.

## Teardown

The teardown output is in `teardown.txt`. Final check at
`2026-11-12T15:37:40Z`: no labeled demo nodes, no `<redacted-pool-*>` machine pools,
no `g5.12xlarge`/`x8i.24xlarge` instances, no demo deployments/jobs/pods or
PVCs, and no demo cache PVs. The three cache volume IDs
(`<redacted-volume-1>`, `<redacted-volume-2>`, `<redacted-volume-3>`) all returned
`InvalidVolume.NotFound`.

Three unrelated 200 GiB `available` EBS volumes remain in the account. Their
tags identify `<redacted-volume-tag-1>` claims in `<redacted-tenant-ns-2>`,
`<redacted-tenant-ns-3>`, and `<redacted-tenant-ns-1>`, on `gp3-csi-retain` PVs; they predate
this run and were left untouched. No demo cache volume remains.
