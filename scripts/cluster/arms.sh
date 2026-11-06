#!/usr/bin/env bash
# Run the four routing policies against the OpenShift stack. Port-forwards the
# trace UI to the local machine, swaps the praxis config per policy (env change
# + rollout), runs the review pipeline through the trace UI so everything is
# recorded, then builds the report. Run from a machine with oc + uv.
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/configs.sh
REPO=$(pwd)
NS="${NS:-openshell-demo}"
RUNS="${RUNS:-runs}"
DOCS="${DOCS:-40}"
# Explicit document ids for demo runs (comma-separated). Overrides DOCS;
# --docs 1 alone picks the first golden document, which is non-sensitive.
DOC_IDS="${DOC_IDS:-}"
DATA="${DATA:-data/synthetic}"
CONCURRENCY="${CONCURRENCY:-12}"
CONFIGS="${CONFIGS:-${ARMS:-all-gpu gpu-small round-robin hybrid}}"
LOCAL_PORT="${LOCAL_PORT:-18080}"
SENSITIVE_PORT="${LOCAL_SENSITIVE_PORT:-18083}"
JUDGMENT_PORT="${LOCAL_JUDGMENT_PORT:-18086}"
SENSITIVE_JUDGMENT_PORT="${LOCAL_SENSITIVE_JUDGMENT_PORT:-18087}"
# Provenance travels with the run. Default to the ROSA cluster this script
# targets; set ENVIRONMENT=sno (or example) for an older stack so it is not
# presented as a measured ROSA result. POOLS_JSON swaps the pool map for
# stacks that are not the ROSA pools (e.g. deploy/openshift/fake-all/
# pools.json for the simulated functional tier).
ENVIRONMENT="${ENVIRONMENT:-rosa}"
CLUSTER="${CLUSTER:-}"
POOLS_JSON="${POOLS_JSON:-$REPO/deploy/openshift/rosa/pools.json}"
prov_args=(--environment "$ENVIRONMENT" --pools "@$POOLS_JSON")
[ -n "$CLUSTER" ] && prov_args+=(--cluster "$CLUSTER")

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

set_config() {
  config=$(config_name "$1")
  oc -n "$NS" set env deploy/praxis "PRAXIS_CONFIG=/etc/praxis/$(config_file "$config")" >/dev/null
  oc -n "$NS" rollout status deploy/praxis --timeout=300s >/dev/null
  # A fresh praxis can serve its first request before its upstream pools
  # are warm. Warm it with one routed request before measuring (the warm-up
  # used to wait for a classifier label; there is no classifier any more).
  for i in $(seq 1 10); do
    curl -s -m 120 -D /tmp/config-h -o /dev/null "$GATEWAY/v1/chat/completions" \
      -H 'Content-Type: application/json' \
      -H "Authorization: Bearer ${TRACE_API_TOKEN:-}" \
      -H "X-API-Key: ${HYBRID_API_KEY_EVAL:-gci-demo-key-eval}" \
      -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"Say ok."}],"max_tokens":8}'
    grep -qi "^x-demo-route:" /tmp/config-h && break
    sleep 2
  done
  echo "praxis now running config $config ($(config_file "$config"))"
}

# Request path via OpenShift Routes by default: port-forwards tunnel through
# the API server and die with it (FINDINGS §7 / D10). Port-forwards remain
# only for the metrics samplers, whose failures cannot corrupt quality
# records. Set USE_ROUTES=0 to fall back to the port-forwarded path.
USE_ROUTES="${USE_ROUTES:-1}"
if [ "$USE_ROUTES" = "1" ]; then
  ensure_route() {
    local name=$1 port=$2
    if ! oc -n "$NS" get route "$name" >/dev/null 2>&1; then
      oc -n "$NS" expose svc/trace-ui --name "$name" --port "$port" >/dev/null
    fi
    # vLLM-class requests exceed the router's default 30s timeout
    # (http_504 at ~30.2s, measured 2026-11-02); match the pipeline's
    # 600s client timeout.
    oc -n "$NS" annotate route "$name" --overwrite \
      haproxy.router.openshift.io/timeout=600s >/dev/null
  }
  ensure_route trace-ui-general 8080
  ensure_route trace-ui-sensitive 8083
  ensure_route trace-ui-judgment 8086
  ensure_route trace-ui-sensitive-judgment 8087
  GATEWAY="http://$(oc -n "$NS" get route trace-ui-general -o jsonpath='{.spec.host}')"
  SENSITIVE="http://$(oc -n "$NS" get route trace-ui-sensitive -o jsonpath='{.spec.host}')"
  JUDGMENT="http://$(oc -n "$NS" get route trace-ui-judgment -o jsonpath='{.spec.host}')"
  SENSITIVE_JUDGMENT="http://$(oc -n "$NS" get route trace-ui-sensitive-judgment -o jsonpath='{.spec.host}')"
else
  oc -n "$NS" port-forward svc/trace-ui "$LOCAL_PORT:8080" "$SENSITIVE_PORT:8083" "$JUDGMENT_PORT:8086" "$SENSITIVE_JUDGMENT_PORT:8087" >/dev/null &
  PF=$!
  trap 'kill $PF 2>/dev/null' EXIT
  GATEWAY="http://127.0.0.1:$LOCAL_PORT"
  SENSITIVE="http://127.0.0.1:$SENSITIVE_PORT"
  JUDGMENT="http://127.0.0.1:$JUDGMENT_PORT"
  SENSITIVE_JUDGMENT="http://127.0.0.1:$SENSITIVE_JUDGMENT_PORT"
fi
# Relay ports, for live metrics sampling during the runs (port-forwards by
# design: a sampler failure loses a metric, never a request).
oc -n "$NS" port-forward svc/trace-ui 18081:8081 18082:8082 18084:8084 18085:8085 >/dev/null &
PF3=$!
trap 'kill ${PF:-} ${PF2:-} $PF3 2>/dev/null' EXIT
for i in $(seq 1 30); do curl -sf "$GATEWAY/_trace/healthz" >/dev/null 2>&1 && break; sleep 1; done

curl -sf -H "Authorization: Bearer ${TRACE_API_TOKEN:-}" -H "X-API-Key: ${HYBRID_API_KEY_EVAL:-gci-demo-key-eval}" "$GATEWAY/v1/models" | grep -q Semantic-Qwen || { echo "gateway not reachable through trace-ui" >&2; exit 1; }

for requested in $CONFIGS; do
  config=$(config_name "$requested")
  out="$RUNS/$config"
  mkdir -p "$out"
  echo
  echo "=== config $config ================================================"
  set_config "$config"
  doc_args=(--docs "$DOCS")
  [ -n "$DOC_IDS" ] && doc_args=(--doc-ids "$DOC_IDS")
  (cd demo && uv run hybrid-run --target gateway \
    --gateway "$GATEWAY" --sensitive-gateway "$SENSITIVE" \
    --judgment-gateway "$JUDGMENT" --sensitive-judgment-gateway "$SENSITIVE_JUDGMENT" \
    --data "$DATA" --out "../$out" --config "$config" \
    "${doc_args[@]}" --concurrency "$CONCURRENCY" \
    "${prov_args[@]}" \
    --sample-metrics vllm-cpu=http://127.0.0.1:18081,vllm-gpu=http://127.0.0.1:18082,vllm-gpu-approved=http://127.0.0.1:18084,vllm-gpu-small=http://127.0.0.1:18085)
done

set_config hybrid
echo
echo "=== report ========================================================="
repo_dir=$(pwd)
run_args=""
for requested in $CONFIGS; do
  run_args="$run_args $repo_dir/$RUNS/$(config_name "$requested")"
done
# shellcheck disable=SC2086
(cd demo && uv run hybrid-score report --runs $run_args \
  --data "$DATA" --out "$repo_dir/$RUNS/report.md") | tail -40
echo
echo "report: $RUNS/report.md (+ .json, .html)"
echo "dashboard: oc -n $NS port-forward svc/trace-ui 8080:8080 -> http://127.0.0.1:8080"
