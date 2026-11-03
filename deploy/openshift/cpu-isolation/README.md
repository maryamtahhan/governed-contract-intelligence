# CPU isolation for the vLLM CPU pools

The pools are Guaranteed QoS in the machine manifests (`requests == limits`,
integer CPU). That stops CFS from throttling them against other pods'
limits, but it does **not** make the CPU set deterministic: the scheduler
can share cores, SMT siblings are still visible, and a pod can span NUMA
nodes. For latency numbers that mean anything, the node must also be
configured. There are two paths; use the first for any run you intend to
compare.

## Recommended: NTO PerformanceProfile (sibling threads offlined)

`performanceprofile.yaml` sets:

- `cpuManagerPolicy: static` — NTO enables it; Guaranteed pods with integer
  CPU requests get **exclusive** CPUs, and Burstable/BestEffort pods
  (gateway, classifier, trace-ui, system, sandbox) share the `reserved` set
  and cannot land on a pool's cores.
- `cpu.reserved` / `cpu.isolated` / `cpu.offlined` — NTO sets the kubelet's
  reserved CPUs from `reserved` and creates a systemd unit that takes
  `offlined` CPUs offline at boot. The isolated CPUs are **thread-0 of whole
  physical cores**; their SMT siblings are in `offlined`, so the pod sees one
  CPU per core and one CPU request is one core. The `offlined` list is
  required — NTO does **not** infer it from the gaps in the other two lists,
  and an online sibling would stay available for Guaranteed allocation.
- `topologyPolicy: single-numa-node` — each pod's CPUs stay on one NUMA
  node. vLLM reads weights from host memory every token; crossing nodes
  makes latency depend on placement.

### The node this was generated from

The SNO (`rhgaudi3s2`) was read on 2026-11-03 — do not assume a two-node
layout, this CPU is split into six:

```
Intel Xeon 6960P, 2 sockets x 72 cores, SMT on = 144 cores / 288 threads
node0  0-23    + 144-167     node3  72-95    + 216-239
node1  24-47   + 168-191     node4  96-119   + 240-263
node2  48-71   + 192-215     node5  120-143  + 264-287
```

`performanceprofile.yaml` therefore isolates 78 cores, one thread-0 CPU per
core: node1 24, node2 24, node3 12, node4 18. The other 66 cores (132
logical CPUs, SMT kept) are reserved — whole nodes 0 and 5 plus the
unisolated remainder of nodes 3 and 4. The 78 siblings of the isolated
cores are in `offlined`.

The three lists were checked against that `lscpu` output: disjoint, full
coverage of CPUs 0–287, and every isolated CPU's sibling is in `offlined`.
Schema and webhook validation without applying:

```bash
oc apply --dry-run=server -f deploy/openshift/cpu-isolation/performanceprofile.yaml
```

### Verify the CPU numbering first

The lists in `performanceprofile.yaml` are for this node's numbering. Some
firmware interleaves siblings (even = thread-0, odd = thread-1) or uses
different NUMA boundaries. Check before applying, and regenerate if the
output differs:

```bash
oc debug node/<node> -- chroot /host lscpu -e=CPU,CORE,SOCKET,NODE,ONLINE
oc debug node/<node> -- chroot /host cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list
oc debug node/<node> -- chroot /host \
  sh -c 'for f in /sys/devices/system/node/node*/cpulist; do echo "$f: $(cat $f)"; done'
```

On this node the sibling of CPU 0 is 144. To regenerate the three lists:
isolated = thread-0 of the cores you want to dedicate; reserved = every
other CPU, with both siblings of each reserved core; offlined = the
siblings of the isolated cores. The three lists must be disjoint and
together cover every logical CPU.

`single-numa-node` drives the sizes: **a 24-core pod needs a whole 24-core
NUMA node**, so the two 7B pools each take one. The smaller pods share the
remaining nodes:

| pool | cores | NUMA node |
|---|---|---|
| `cpureal-vllm-gpu` (7B, 24) | 24 | node1 (24-47) |
| `cpureal-vllm-gpu-approved` (7B, 24) | 24 | node2 (48-71) |
| `cpureal-vllm-gpu-small` (1.5B, 12) | 12 | node3 (72-83) |
| `cpureal-vllm-cpu` (1.5B, 12 / MoE, 16) | 12 / 16 | node4 (96-107 / 96-111) |
| specialist (2) | 2 | node4 (112-113) |

### Apply

```bash
oc apply -f deploy/openshift/cpu-isolation/performanceprofile.yaml
oc get mcp                     # wait for UPDATED=True and UPDATING=False
oc get performanceprofile cpu-isolation -o yaml | grep -A4 status
```

On the SNO this reboots the only node. Do it **before** a measurement
series, never during one. After the rollout:

```bash
# The offlined siblings should be exactly the CPUs in spec.cpu.offlined.
oc debug node/<node> -- chroot /host cat /sys/devices/system/cpu/offline
oc debug node/<node> -- chroot /host lscpu -e=CPU,CORE,SOCKET,NODE,ONLINE
```

## What the requests mean

Kubernetes counts **logical** CPUs. With the offlining profile, each request
is a **physical core**, and the pool manifests carry physical counts:

| pool | CPU request (physical cores) | memory |
|---|---|---|
| `cpureal-vllm-gpu` (7B) | 24 | 64Gi |
| `cpureal-vllm-gpu-approved` (7B) | 24 | 64Gi |
| `cpureal-vllm-gpu-small` (1.5B) | 12 | 20Gi |
| `cpureal-vllm-cpu` (1.5B) | 12 | 20Gi |
| `cpu-real-moe` CPU pool (35B-A3B) | 16 | 128Gi |
| `cpu-real-9b` CPU pool (9B) | 16 | 64Gi |
| specialist (RoBERTa) | 2 | 4Gi |

Base total: 24+24+12+12+2 = **74** of the 78 isolated cores; 4 cores stay
idle on node4. The MoE/9B variants swap the 12-core CPU pool for 16 and use
**78 of 78** — an exact fit, with each pod still inside one NUMA node. The
earlier 48/48/24/24 requests were **logical** CPUs under `full-pcpus-only`
— the same 24/24/12/12 physical cores. If you use the fallback, double the
numbers.

The base KServe manifest (`manifests/vllm-cpu-inferenceservice.yaml`) uses 8
logical CPUs (4 physical under `full-pcpus-only`) because on a GPU node the
CPU pool serves only the low-volume tier. If that node also gets the
offlining profile, halve its request to 4 physical cores.

## Hugepages

Explicit hugetlbfs pages are **not** enabled. As of vLLM main, the CPU
backend allocates weights and the KV cache with ordinary anonymous memory
and has no hugetlbfs allocation path (grep the vLLM source: only the UMBP KV
connector and one DeepSeek model call `MADV_HUGEPAGE`). Reserving
`hugepages` in the PerformanceProfile and requesting `hugepages-2Mi` in the
pods would strand memory that no process can use.

The lever that can affect vLLM's allocations is **Transparent Huge Pages**.
NTO performance profiles set THP to `never` for latency determinism;
`thp-experiment.yaml` is an opt-in MachineConfig that sets it back to
`always` after `tuned.service`. It is an experiment, not a default:

```bash
oc apply -f deploy/openshift/cpu-isolation/thp-experiment.yaml
oc get mcp                     # SNO reboots again
cat /sys/kernel/mm/transparent_hugepage/enabled     # [always] ...
grep AnonHugePages /proc/meminfo
oc exec deploy/cpureal-vllm-cpu -- grep AnonHugePages /proc/1/smaps_rollup
```

THP can cut TLB misses for the large weight/KV working set, but khugepaged
and direct compaction can add p99 stalls. Measure p99, not just the mean;
revert with `oc delete mc 99-thp-always` if variance worsens. If a future
vLLM version adds an explicit hugetlbfs path, add `hugepages.pages` and the
matching pod requests together — never reserve pages without a consumer.

## Verify a running pod

```bash
# Guaranteed, and request == limit:
oc -n openshell-demo get pod -l demo.openshell.ai/pool=vllm-cpu \
  -o jsonpath='{range .items[*]}{.metadata.name}{" qos="}{.status.qosClass}{" cpu="}{.spec.containers[0].resources.requests.cpu}{"/"}{.spec.containers[0].resources.limits.cpu}{"\n"}{end}'

# The cpuset the container actually sees: N CPUs, one per physical core.
oc -n openshell-demo exec deploy/cpureal-vllm-cpu -- \
  cat /sys/fs/cgroup/cpuset.cpus.effective
oc -n openshell-demo exec deploy/cpureal-vllm-cpu -- lscpu

# Map those CPU ids to NUMA nodes and siblings on the host:
oc debug node/<node> -- chroot /host lscpu -e=CPU,CORE,SOCKET,NODE,ONLINE
```

A correct pod has `qos=Guaranteed`, equal request/limit, a cpuset of exactly
that many CPUs, every CPU on one NUMA node, and **no sibling pairs** in the
set (siblings are offline). If the set contains both 3 and 147, the profile
did not take effect — do not record latency numbers from that run.

Changing any request changes the cpuset and invalidates comparisons with
earlier runs. Re-measure; do not splice.
