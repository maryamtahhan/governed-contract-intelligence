#!/usr/bin/env bash
# Direct-arm quality runs against the raw model pools, bypassing praxis.
#
# "Direct" is the single-model baseline: one checkpoint answers classify,
# every extraction and the risk step. "Stacked" (scripts/cluster/arms.sh) is
# the composite pipeline. Quality and sensitivity-class numbers come from the
# direct arm because routing cannot contaminate them; routing and governance
# come from the pipeline arms. Run both to answer "is the composite worth it".
#
#   scripts/cluster/direct.sh                       # 200 synthetic docs, default pools
#   DOC_IDS=C-2026-0001,C-2026-0003 scripts/cluster/direct.sh   # demo subset
#   DATA=data/cuad-test DOCS=19 scripts/cluster/direct.sh
#   POOLS="cpu-moe=cpureal-vllm-cpu,gpu-moe=gpumoe-vllm" scripts/cluster/direct.sh
#
# Port-forwards are used because the pools are not exposed through Routes on
# purpose (a raw model endpoint must not be publicly reachable). A dropped
# forward surfaces as transport_* outcomes in the run, never as a silent
# quality change; re-run the affected pool if errors > 0.
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO=$(pwd)
NS="${NS:-openshell-demo}"
DATA="${DATA:-data/synthetic}"
DOCS="${DOCS:-200}"
DOC_IDS="${DOC_IDS:-}"
RUNS="${RUNS:-runs/direct}"
CONCURRENCY="${CONCURRENCY:-4}"
ENVIRONMENT="${ENVIRONMENT:-rosa}"
CLUSTER="${CLUSTER:-}"
# name=service[:port] pairs; the name becomes the run-dir name.
POOLS="${POOLS:-cpu-moe=cpureal-vllm-cpu,gpu-moe=gpumoe-vllm,cpu-small=cpureal-vllm-gpu-small}"
LOCAL_BASE="${LOCAL_BASE:-19100}"

for c in oc curl uv; do command -v "$c" >/dev/null || { echo "missing: $c" >&2; exit 1; }; done
oc whoami >/dev/null
mkdir -p "$RUNS"

doc_args=(--docs "$DOCS")
[ -n "$DOC_IDS" ] && doc_args=(--doc-ids "$DOC_IDS")
prov_args=(--environment "$ENVIRONMENT" --pools "@$REPO/deploy/openshift/rosa/pools.json")
[ -n "$CLUSTER" ] && prov_args+=(--cluster "$CLUSTER")

pids=()
cleanup() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

i=0
names=()
for entry in ${POOLS//,/ }; do
  name=${entry%%=*}
  svc=${entry#*=}
  svc=${svc%%:*}
  port=$((LOCAL_BASE + i))
  i=$((i + 1))
  names+=("$name")
  echo "== $name: svc/$svc -> 127.0.0.1:$port"
  oc -n "$NS" port-forward "svc/$svc" "$port:8000" >/dev/null 2>&1 &
  pids+=($!)
  ready=0
  for _ in $(seq 1 90); do
    if curl -sf -m 2 "http://127.0.0.1:$port/v1/models" >/dev/null 2>&1; then
      ready=1; break
    fi
    sleep 1
  done
  if [ "$ready" != 1 ]; then
    echo "svc/$svc did not answer /v1/models on 127.0.0.1:$port" >&2
    exit 1
  fi
  (cd demo && uv run hybrid-run --target direct \
    --endpoint "http://127.0.0.1:$port/v1" --data "$DATA" \
    --out "../$RUNS/$name" "${doc_args[@]}" \
    --concurrency "$CONCURRENCY" "${prov_args[@]}")
done

echo
echo "== direct report =="
report_runs=()
for name in "${names[@]}"; do report_runs+=("$REPO/$RUNS/$name"); done
(cd demo && uv run hybrid-score report --runs "${report_runs[@]}" \
  --data "$DATA" --out "$REPO/$RUNS/report.md") | tail -20
echo
echo "report: $RUNS/report.md"
