#!/usr/bin/env bash
# Pull the plug on a GPU pool mid-run (the on-stage finale), or restore it.
#   scripts/demo/pull-the-plug.sh                 # stop the general GPU pool
#   scripts/demo/pull-the-plug.sh --restore       # bring it back
#   scripts/demo/pull-the-plug.sh --pool vllm-gpu-approved   # kill the approved pool
set -euo pipefail
NS="${NS:-openshell-demo}"
POOL="${POOL:-vllm-gpu}"
RESTORE=false
for a in "$@"; do
  case "$a" in
    --restore) RESTORE=true ;;
    --pool) shift_ok=1 ;;
    *) POOL="$a" ;;
  esac
done
if $RESTORE; then
  echo "scaling $POOL-predictor back to 1"
  oc -n "$NS" scale "deploy/$POOL-predictor" --replicas=1
  oc -n "$NS" rollout status "deploy/$POOL-predictor" --timeout=600s
else
  echo "scaling $POOL-predictor to zero"
  oc -n "$NS" scale "deploy/$POOL-predictor" --replicas=0
  echo "praxis health checks will mark it unhealthy within ~15s;"
  echo "watch: oc -n $NS port-forward svc/trace-ui 8080:8080  -> http://127.0.0.1:8080"
fi
