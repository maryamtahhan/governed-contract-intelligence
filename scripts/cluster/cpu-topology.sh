#!/usr/bin/env bash
# Read a cluster node's CPU/NUMA topology and propose per-pool
# VLLM_CPU_OMP_THREADS_BIND core lists for the ROSA CPU-inference node.
#
# Why this exists: on ROSA there is no static CPU manager (no PerformanceProfile,
# no exclusive cpusets), so placement is done the way the Intel llm-d article
# describes - explicit core lists per pool. vLLM's own "auto" mode binds every
# rank-0 worker to NUMA node 0 (vllm/utils/ompmultiprocessing.py,
# allowed_numa_nodes[local_rank]), which would pile all single-replica pools onto
# the same cores. This script derives disjoint, NUMA-local lists instead.
#
# Rules encoded (verified against vLLM 0.33.0, the docker.io/vllm/vllm-openai-cpu
# image we deploy):
#   - bind THREAD-0 of each chosen physical core only (one OMP thread per core;
#     siblings stay online on ROSA but are left out of the bind list)
#   - a pool's cores must come from a single NUMA node (weights are read from
#     host memory every token; crossing nodes makes latency placement-dependent)
#   - cores not in any list are left for housekeeping (kubelet, DS pods, the
#     scheduler process - vLLM's own unpinned threads)
#
# Usage: cpu-topology.sh <node-name> [--pools name=cores,name=cores]
#   default pools: cpureal-vllm-cpu=16,cpureal-vllm-gpu-small=2,specialist=2
#                  (MoE 16 physical cores - the SNO-comparable layout; the 1.5B
#                   pool 12; specialist 2; 7B pools stay parked at 0 replicas)
# Output: human-readable proposal + JSON on stdout (consume with jq or redirect).
set -euo pipefail

NODE="${1:?usage: cpu-topology.sh <node-name> [--pools name=cores,...]}"
shift || true
POOLS="${POOLS:-cpureal-vllm-cpu=16,cpureal-vllm-gpu-small=12,specialist=2}"
while [ $# -gt 0 ]; do
  case "$1" in
    --pools) POOLS="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

echo "collecting topology from node $NODE ..." >&2
oc debug "node/${NODE}" -- chroot /host sh -c '
  lscpu -p=CPU,CORE,SOCKET,NODE,ONLINE
  echo ===SIBLINGS===
  for f in /sys/devices/system/cpu/cpu*/topology/thread_siblings_list; do
    echo "${f%/topology/*}: $(cat $f)"
  done
  echo ===FLAGS===
  grep -m1 ^flags /proc/cpuinfo | tr " " "\n" | grep -E "^amx|^avx512" | sort -u
' > /tmp/cpu-topology-raw.txt 2>/dev/null

python3 - "$POOLS" /tmp/cpu-topology-raw.txt <<'EOF'
import json, re, sys

# argv[1] = pool spec, argv[2] = raw topology file (read from the file, not
# stdin: stdin carries this program via the here-document).
raw = open(sys.argv[2]).read()
lscpu_part, _, rest = raw.partition("===SIBLINGS===")
sib_part, _, flags_part = rest.partition("===FLAGS===")

# lscpu -p: "CPU,Core,Socket,Node,Online" with # comment lines. Online is
# spelled Y/N on RHEL 9 (util-linux 2.37), 1/0 on newer util-linux.
cpus = []
for line in lscpu_part.splitlines():
    if not line or line.startswith("#"):
        continue
    cpu, core, socket, node, online = line.split(",")
    if online.strip().upper() not in ("Y", "1", "YES", "TRUE"):
        continue
    cpus.append(dict(id=int(cpu), core=int(core), socket=int(socket), node=int(node)))

# A physical core = (socket, core); thread-0 = lowest logical id of that core.
phys = {}
for c in cpus:
    key = (c["socket"], c["core"])
    phys.setdefault(key, dict(node=c["node"], threads=[]))["threads"].append(c["id"])
cores = []
for (socket, core), v in phys.items():
    v["threads"].sort()
    cores.append(dict(socket=socket, core=core, node=v["node"], t0=v["threads"][0],
                      ht=len(v["threads"])))
cores.sort(key=lambda c: c["t0"])

nodes = sorted({c["node"] for c in cores})
by_node = {n: [c for c in cores if c["node"] == n] for n in nodes}
flags = sorted(flags_part.split())

def fmt(ids):
    """Compress [0,1,2,5,6] -> '0-2,5-6' (parse_id_list accepts both)."""
    out, start, prev = [], None, None
    for i in sorted(ids):
        if start is None:
            start = prev = i; continue
        if i == prev + 1:
            prev = i; continue
        out.append(f"{start}-{prev}" if prev > start else f"{start}")
        start = prev = i
    if start is not None:
        out.append(f"{start}-{prev}" if prev > start else f"{start}")
    return ",".join(out)

pools = []
for spec in sys.argv[1].split(","):
    name, n = spec.split("=")
    pools.append(dict(name=name, want=int(n)))

free = {n: [c["t0"] for c in by_node[n]] for n in nodes}
layout, problems = [], []
for p in pools:
    # Best-fit: the tightest node that still holds the pool. This keeps
    # small pools beside the pool they talk to (the specialist next to the
    # MoE) and leaves the other node's cores as one contiguous block for a
    # future pool instead of scattering holes across both nodes.
    cand = [n for n in nodes if len(free[n]) >= p["want"]]
    if not cand:
        problems.append(f"{p['name']}: wants {p['want']} cores on one NUMA node; "
                        f"free per node: " + ", ".join(f"node{n}={len(free[n])}" for n in nodes))
        layout.append(dict(name=p["name"], error="does not fit"))
        continue
    n = min(cand, key=lambda n: len(free[n]))
    ids = sorted(free[n])[: p["want"]]
    free[n] = [i for i in free[n] if i not in ids]
    layout.append(dict(name=p["name"], cores=p["want"], numa_node=n,
                       bind=fmt(ids), thread0_ids=ids))

result = dict(
    node=dict(physical_cores=len(cores), logical=len(cpus), smt=len(cpus) // len(cores),
              numa_nodes={n: len(by_node[n]) for n in nodes},
              numa_thread0={n: fmt(c["t0"] for c in by_node[n]) for n in nodes},
              amx=[f for f in flags if f.startswith("amx")],
              avx512=[f for f in flags if f.startswith("avx512")]),
    layout=layout,
    free_per_node={n: len(free[n]) for n in nodes},
    problems=problems,
)
print(json.dumps(result, indent=2))

print("\n--- proposal ---", file=sys.stderr)
for p in layout:
    if "error" in p:
        print(f"  {p['name']:<28} DOES NOT FIT: {p['error']}", file=sys.stderr)
    else:
        print(f"  {p['name']:<28} {p['cores']:>2} cores on NUMA node {p['numa_node']}: "
              f"VLLM_CPU_OMP_THREADS_BIND={p['bind']}", file=sys.stderr)
print(f"  housekeeping (unpinned thread-0 per node): "
      f"{ {f'node{n}': len(free[n]) for n in nodes} }", file=sys.stderr)
if problems:
    print("\nPROBLEMS:\n  " + "\n  ".join(problems), file=sys.stderr)
    sys.exit(1)
EOF
