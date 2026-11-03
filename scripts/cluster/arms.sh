#!/usr/bin/env bash
# Run the measurement arms against the OpenShift stack. Port-forwards the
# trace UI to the local machine, swaps the praxis config per arm (env change
# + rollout), runs the review pipeline through the trace UI so everything is
# recorded, then builds the report. Run from a machine with oc + uv.
set -euo pipefail
cd "$(dirname "$0")/../.."
NS="${NS:-openshell-demo}"
RUNS="${RUNS:-runs}"
DOCS="${DOCS:-40}"
DATA="${DATA:-data/synthetic}"
CONCURRENCY="${CONCURRENCY:-12}"
ARMS="${ARMS:-A B C D}"
LOCAL_PORT="${LOCAL_PORT:-18080}"
SENSITIVE_PORT="${LOCAL_SENSITIVE_PORT:-18083}"

for c in oc curl uv; do command -v "$c" >/dev/null || { echo "missing: $c" >&2; exit 1; }; done
# Stale port-forwards from earlier sessions hold the ports and kill the run.
pkill -f "port-forward svc/trace-ui" 2>/dev/null || true
sleep 2
oc whoami >/dev/null || { echo "not logged in" >&2; exit 1; }
oc -n "$NS" get deploy/praxis >/dev/null 2>&1 || { echo "stack not deployed: scripts/02-apply.sh" >&2; exit 1; }

if [ "$DATA" = "data/synthetic" ] && [ ! -f demo/data/synthetic/ground_truth.json ]; then
  echo "generating synthetic documents"
  (cd demo && uv run hybrid-gendata --out data/synthetic --count 200 --seed 42 --golden 20)
fi
if [ ! -f "demo/$DATA/ground_truth.json" ]; then
  echo "dataset not found: demo/$DATA" >&2; exit 1
fi

arm_config() { case "$1" in A) echo arm-a.yaml;; B) echo arm-b.yaml;; C) echo arm-c.yaml;; *) echo config.yaml;; esac; }

set_arm() {
  oc -n "$NS" set env deploy/praxis "PRAXIS_CONFIG=/etc/praxis/$(arm_config "$1")" >/dev/null
  oc -n "$NS" rollout status deploy/praxis --timeout=300s >/dev/null
  # A fresh praxis can serve its first request through a cold gRPC
  # connection to llm-d-sc (classification skipped, no label). Warm it with
  # one request before measuring.
  for i in $(seq 1 10); do
    curl -s -m 120 -D /tmp/arm-h -o /dev/null "$GATEWAY/v1/chat/completions" \
      -H 'Content-Type: application/json' \
      -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"Say ok."}],"max_tokens":8}'
    grep -qi "^x-demo-classification:" /tmp/arm-h && break
    sleep 2
  done
  echo "praxis now running arm $1 ($(arm_config "$1"))"
}

oc -n "$NS" port-forward svc/trace-ui "$LOCAL_PORT:8080" >/dev/null &
PF=$!
trap 'kill $PF 2>/dev/null' EXIT
oc -n "$NS" port-forward svc/trace-ui "$SENSITIVE_PORT:8083" >/dev/null &
PF2=$!
trap 'kill $PF $PF2 2>/dev/null' EXIT
# Relay ports, for live metrics sampling during the runs.
oc -n "$NS" port-forward svc/trace-ui 18081:8081 18082:8082 18084:8084 18085:8085 >/dev/null &
PF3=$!
trap 'kill $PF $PF2 $PF3 2>/dev/null' EXIT
for i in $(seq 1 30); do curl -sf "http://127.0.0.1:$LOCAL_PORT/_trace/healthz" >/dev/null 2>&1 && break; sleep 1; done

GATEWAY="http://127.0.0.1:$LOCAL_PORT"
SENSITIVE="http://127.0.0.1:$SENSITIVE_PORT"
curl -sf "$GATEWAY/v1/models" | grep -q Semantic-Qwen || { echo "gateway not reachable through trace-ui" >&2; exit 1; }

for arm in $ARMS; do
  out="$RUNS/arm-$arm"
  mkdir -p "$out"
  echo
  echo "=== arm $arm ====================================================="
  set_arm "$arm"
  (cd demo && uv run hybrid-review \
    --gateway "$GATEWAY" --sensitive-gateway "$SENSITIVE" \
    --data "$DATA" --out "../$out" --arm "$arm" \
    --docs "$DOCS" --concurrency "$CONCURRENCY" \
    --sample-metrics vllm-cpu=http://127.0.0.1:18081,vllm-gpu=http://127.0.0.1:18082,vllm-gpu-approved=http://127.0.0.1:18084,vllm-gpu-small=http://127.0.0.1:18085)
done

set_arm D
echo
echo "=== report ========================================================="
repo_dir=$(pwd)
run_args=""
for arm in $ARMS; do run_args="$run_args $repo_dir/$RUNS/arm-$arm"; done
# shellcheck disable=SC2086
(cd demo && uv run hybrid-score report --runs $run_args \
  --data "$DATA" --out "$repo_dir/$RUNS/report.md") | tail -40
echo
echo "report: $RUNS/report.md (+ .json, .html)"
echo "dashboard: oc -n $NS port-forward svc/trace-ui 8080:8080 -> http://127.0.0.1:8080"
