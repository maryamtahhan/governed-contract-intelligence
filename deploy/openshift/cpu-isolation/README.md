# CPU isolation for the vLLM CPU pool

The pools are Guaranteed QoS in the machine manifests (`requests == limits`,
integer CPU). That alone stops CFS from throttling them against other pods'
limits, but it does **not** stop the scheduler from placing another pod on
the same cores, and it says nothing about SMT or NUMA. For latency numbers
that mean anything, the node must also be configured:

```bash
# Check what the node is doing today.
oc get kubeletconfig -o yaml | grep -E 'cpuManagerPolicy|full-pcpus-only|topologyManager'
oc get nodes -o custom-columns=NAME:.metadata.name,CPU:.status.allocatable.cpu

# Opt in (reboots the node on SNO; see the warning below).
oc apply -f deploy/openshift/cpu-isolation/kubeletconfig.yaml
oc get mcp            # wait for UPDATED=True, then verify the pools came back
```

`kubeletconfig.yaml` sets:

- `cpuManagerPolicy: static` — Guaranteed pods with integer CPU requests get
  **exclusive** CPUs; Burstable/BestEffort pods (gateway, classifier,
  trace-ui, system) share the remaining pool and cannot land on a pool's
  cores.
- `cpuManagerPolicyOptions.full-pcpus-only: "true"` — allocate whole
  physical cores, not lone hyperthreads. vLLM's CPU backend binds its OMP
  threads to **physical** cores and does not use an SMT sibling, so a pod
  that receives only sibling threads uses half of what Kubernetes thinks it
  reserved. This option also makes the cpuset symmetric.
- `topologyManagerPolicy: single-numa-node`, scope `pod` — keep the pod's
  CPUs (and memory, when the memory manager is enabled) on one NUMA node.
  vLLM reads weights from host memory every token; crossing NUMA costs
  bandwidth and makes latency depend on placement.

Change `machineConfigPoolSelector` from `master` to `worker` if the pools run
on worker nodes. On a multi-node cluster the rollout is rolling; on the SNO
used for the recorded runs it reboots the only node, so do it **before** a
measurement series, never during one.

## What the requests mean

Kubernetes counts **logical** CPUs. The SNO has SMT enabled (288 logical
CPUs, 144 physical cores), so with `full-pcpus-only` each guarantee is a
whole-core allocation:

| pool | CPU request (logical) | physical cores | memory |
|---|---|---|---|
| `cpureal-vllm-gpu` (7B) | 48 | 24 | 64Gi |
| `cpureal-vllm-gpu-approved` (7B) | 48 | 24 | 64Gi |
| `cpureal-vllm-gpu-small` (1.5B) | 24 | 12 | 20Gi |
| `cpureal-vllm-cpu` (1.5B) | 24 | 12 | 20Gi |
| `cpu-real-moe` CPU pool (35B-A3B) | 32 | 16 | 128Gi |
| `cpu-real-9b` CPU pool (9B) | 32 | 16 | 64Gi |
| specialist (RoBERTa) | 4 | 2 | 4Gi |

Base total: 144 logical / 72 physical cores of 288/144. The MoE variant is
152 logical / 76 physical. The remainder is the shared pool for everything
else, so the pools do not overlap each other or the gateway.

The base KServe manifest (`manifests/vllm-cpu-inferenceservice.yaml`) uses 8
logical CPUs (4 physical) because on a GPU node the CPU pool serves only the
low-volume tier.

## Verify a running pod

```bash
# Guaranteed, and request == limit:
oc -n openshell-demo get pod -l demo.openshell.ai/pool=vllm-cpu \
  -o jsonpath='{range .items[*]}{.metadata.name}{" qos="}{.status.qosClass}{" cpu="}{.spec.containers[0].resources.requests.cpu}{"/"}{.spec.containers[0].resources.limits.cpu}{"\n"}{end}'

# The cpuset the container actually sees (exclusive set, whole cores):
oc -n openshell-demo exec deploy/cpureal-vllm-cpu -- \
  cat /sys/fs/cgroup/cpuset.cpus.effective

# Map those CPU ids to NUMA nodes and siblings on the host:
oc debug node/<node> -- chroot /host lscpu -e=CPU,CORE,SOCKET,NODE,ONLINE
```

A correct pod has `qos=Guaranteed`, equal request/limit, a cpuset of exactly
that many logical CPUs, and every CPU in the set on one NUMA node with both
SMT siblings present. If the set is split or contains lone siblings, the
KubeletConfig did not take effect (check `oc get mcp` and the node's
`KubeletConfig` condition) — do not record latency numbers from that run.

Changing any request changes the cpuset and invalidates comparisons with
earlier runs. Re-measure; do not splice.
