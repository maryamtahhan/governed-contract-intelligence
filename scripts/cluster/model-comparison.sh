#!/usr/bin/env bash
# CPU-pool model comparison: swap the CPU pool to a model variant, wait for
# it to be healthy and warm, then rerun arm D (the full matrix) on the same
# documents into its own run dir so the scorer can compare CPU-pool models.
#
#   scripts/cluster/model-comparison.sh moe   # Qwen3.5-35B-A3B
#   scripts/cluster/model-comparison.sh 9b    # Qwen3.5-9B dense
#   scripts/cluster/model-comparison.sh base  # back to Qwen2.5-1.5B
#
# Results land in ${RUNS:-runs/cuad-mixed}/arm-D-<variant>; compare with
#   (cd demo && uv run hybrid-score report --runs ../runs/cuad-mixed/arm-D \
#        --runs ../runs/cuad-mixed/arm-D-moe --data "$DATA" --out ../model-comparison.md)
set -euo pipefail
cd "$(dirname "$0")/../.."
NS="${NS:-openshell-demo}"
VARIANT="${1:?usage: model-comparison.sh moe|9b|base}"
DATA="${DATA:-data/cuad-docs}"
RUNS="${RUNS:-runs/cuad-mixed}"
DOCS="${DOCS:-20}"
CONCURRENCY="${CONCURRENCY:-12}"
LOCAL_PORT="${LOCAL_PORT:-18080}"
SENSITIVE_PORT="${LOCAL_SENSITIVE_PORT:-18083}"

case "$VARIANT" in
  moe)        OVERLAY=deploy/openshift/cpu-real-moe; POOL_DEPLOY=cpureal-vllm-cpu ;;
  moe-thinking) OVERLAY=deploy/openshift/cpu-real-moe; POOL_DEPLOY=cpureal-vllm-cpu; export HYBRID_THINKING=1 ;;
  9b)         OVERLAY=deploy/openshift/cpu-real-9b; POOL_DEPLOY=cpureal-vllm-cpu ;;
  specialist) OVERLAY=deploy/openshift/cpu-real-specialist; POOL_DEPLOY=cpureal-specialist ;;
  specialist-moe) OVERLAY=deploy/openshift/cpu-real-specialist-moe; POOL_DEPLOY=cpureal-specialist ;;
  base)       OVERLAY=deploy/openshift/cpu-real; POOL_DEPLOY=cpureal-vllm-cpu ;;
  *) echo "unknown variant: $VARIANT" >&2; exit 2 ;;
esac
# thinking mode must never leak into non-thinking variants
[[ "$VARIANT" == moe-thinking ]] || unset HYBRID_THINKING

if [[ "$VARIANT" == specialist* ]]; then
  if ! oc -n "$NS" get bc hybrid-specialist >/dev/null 2>&1; then
    echo "== building the specialist image (checkpoint baked in at build time)"
    oc -n "$NS" new-build --binary --strategy docker --name hybrid-specialist >/dev/null
    oc -n "$NS" patch bc hybrid-specialist --type merge \
      -p '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"Dockerfile.specialist"}}}}'
  fi
  if ! oc -n "$NS" get imagestreamtag hybrid-specialist:latest >/dev/null 2>&1; then
    echo "== building the specialist image (minimal context; checkpoint from Zenodo)"
    # Stage a minimal build context: --from-dir=demo uploads ~10GB of local
    # model caches and venv and dies mid-upload (the 2026-10-31 failure).
    # Dockerfile.specialist needs only these files; the checkpoint itself is
    # downloaded from Zenodo inside the build.
    STAGE="$(mktemp -d)"
    cp demo/Dockerfile.specialist demo/pyproject.toml demo/README.md "$STAGE/"
    cp -R demo/src "$STAGE/src"
    oc -n "$NS" start-build hybrid-specialist --from-dir="$STAGE" --follow 2>&1 | tail -2
    rm -rf "$STAGE"
  fi
fi

echo "== applying $OVERLAY"
oc apply -k "$OVERLAY" >/dev/null

echo -n "== waiting for the CPU pool to become healthy (72GB downloads take a while)"
for i in $(seq 1 480); do
  # python3, not curl: the specialist image is python-slim and ships no curl
  if oc -n "$NS" exec deploy/$POOL_DEPLOY -- python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" >/dev/null 2>&1; then
    code=200
  else
    code=000
  fi
  [ "$code" = 200 ] && { echo " healthy"; break; }
  echo -n .
  sleep 10
done
[ "$code" = 200 ] || { echo; echo "$POOL_DEPLOY did not become healthy" >&2; exit 1; }

echo -n "== warming the pool"
oc -n "$NS" exec deploy/trace-ui -- python3 -c "
import json, urllib.request
pool = '$POOL_DEPLOY'
prompts = [
    {'role': 'user', 'content': 'Say ok.'},
    {'role': 'user', 'content': 'Please extract the termination clause from the following contract text and quote it exactly. Return JSON.'},
]
for msg in prompts:
    body = json.dumps({'model': 'Semantic-Qwen', 'max_tokens': 16, 'messages': [msg]}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f'http://{pool}:8000/v1/chat/completions', data=body,
        headers={'Content-Type': 'application/json'}), timeout=1800).read()
print(' warm')"

# Request path via OpenShift Routes by default: port-forwards tunnel
# through the API server and die with it (the 2026-11-02 tunnel death
# contaminated two arms - FINDINGS §7). Port-forwards remain only for the
# metrics samplers, whose failures do not affect quality records.
# Set USE_ROUTES=0 to fall back to the port-forwarded request path.
if [[ "${USE_ROUTES:-1}" = 1 ]]; then
  ensure_route() {
    local name=$1 port=$2
    if ! oc -n "$NS" get route "$name" >/dev/null 2>&1; then
      oc -n "$NS" expose svc/trace-ui --name "$name" --port "$port" >/dev/null
    fi
    # vLLM-class requests exceed the router's default 30s timeout
    # (http_504 at ~30.2s, measured 2026-11-02); match the pipeline's
    # 600s client timeout
    oc -n "$NS" annotate route "$name" --overwrite haproxy.router.openshift.io/timeout=600s >/dev/null
  }
  ensure_route trace-ui-general 8080
  ensure_route trace-ui-sensitive 8083
  GATEWAY_HOST=$(oc -n "$NS" get route trace-ui-general -o jsonpath='{.spec.host}')
  GATEWAY="http://$GATEWAY_HOST"
  SENSITIVE_GATEWAY="http://$(oc -n "$NS" get route trace-ui-sensitive -o jsonpath='{.spec.host}')"
  for i in $(seq 1 30); do curl -sf "$GATEWAY/_trace/healthz" >/dev/null 2>&1 && break; sleep 1; done
  curl -sf "$GATEWAY/_trace/healthz" >/dev/null || { echo "route to trace-ui not healthy" >&2; exit 1; }
else
  oc -n "$NS" port-forward svc/trace-ui "$LOCAL_PORT:8080" "$SENSITIVE_PORT:8083" >/dev/null &
  PF=$!
  trap 'kill $PF 2>/dev/null' EXIT
  GATEWAY="http://127.0.0.1:$LOCAL_PORT"
  SENSITIVE_GATEWAY="http://127.0.0.1:$SENSITIVE_PORT"
  for i in $(seq 1 30); do curl -sf "$GATEWAY/_trace/healthz" >/dev/null 2>&1 && break; sleep 1; done
fi
oc -n "$NS" port-forward svc/trace-ui 18081:8081 18082:8082 18084:8084 18085:8085 >/dev/null &
PF2=$!
trap 'kill $PF2 2>/dev/null' EXIT

oc -n "$NS" set env deploy/praxis "PRAXIS_CONFIG=/etc/praxis/config.yaml" >/dev/null
oc -n "$NS" rollout status deploy/praxis --timeout=300s >/dev/null
for i in $(seq 1 10); do
  curl -s -m 300 -D /tmp/mc-h -o /dev/null "$GATEWAY/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer ${TRACE_API_TOKEN:-}" \
    -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"Say ok."}],"max_tokens":8}'
  grep -qi "^x-demo-classification:" /tmp/mc-h && break
  sleep 2
done

out="$RUNS/arm-D-$VARIANT"
mkdir -p "$out"
echo "== running arm D with the $VARIANT CPU pool ($DATA, $DOCS docs)"
# optional extra hybrid-review args; a plain string (not an array) because
# macOS bash 3.2 + set -u treats empty-array expansion as unbound
REVIEW_EXTRA=""
if [[ "$VARIANT" == moe-thinking ]]; then
  # thinking generates many extra tokens: give each request room and avoid
  # pool contention (the A/B is about quality, not throughput)
  REVIEW_EXTRA="--timeout 3600 --concurrency 4"
fi
(cd demo && uv run hybrid-review \
  --gateway "$GATEWAY" --sensitive-gateway "$SENSITIVE_GATEWAY" \
  --data "$DATA" --out "../$out" --arm D --docs "$DOCS" --concurrency "$CONCURRENCY" \
  $REVIEW_EXTRA \
  --sample-metrics vllm-cpu=http://127.0.0.1:18081,vllm-gpu=http://127.0.0.1:18082,vllm-gpu-approved=http://127.0.0.1:18084,vllm-gpu-small=http://127.0.0.1:18085)

echo
echo "done: $out"
echo "compare with:"
echo "  (cd demo && uv run hybrid-score report --runs ../$RUNS/arm-D --runs ../$out \\"
echo "       --data $DATA --out ../model-comparison-$VARIANT.md)"
