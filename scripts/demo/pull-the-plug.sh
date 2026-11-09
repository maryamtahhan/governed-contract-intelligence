#!/usr/bin/env bash
# Pull the plug on a GPU pool mid-run (the on-stage finale), or restore it.
#   scripts/demo/pull-the-plug.sh                 # stop the general GPU pool
#   scripts/demo/pull-the-plug.sh --restore       # bring it back
#   scripts/demo/pull-the-plug.sh --pool vllm-gpu-approved   # kill the approved pool
#   scripts/demo/pull-the-plug.sh --pool=vllm-gpu-small      # same, = form
#
# Resolves the deployment for either deployment shape: KServe RawDeployment
# (<pool>-predictor) or the cpu-real overlays (cpureal-<pool>).
set -euo pipefail
NS="${NS:-openshell-demo}"
POOL="${POOL:-vllm-gpu}"
RESTORE=false

usage() { sed -n '2,9p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --restore) RESTORE=true; shift ;;
    --pool)
      [ $# -ge 2 ] || { echo "--pool needs a value" >&2; exit 2; }
      POOL="$2"; shift 2 ;;
    --pool=*) POOL="${1#--pool=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# Candidate deployments, most specific first. On the ROSA overlay the two GPU
# routes share one Deployment (gpumoe-vllm); the cpureal-vllm-gpu* objects
# also exist there but are parked at replicas 0, so trying them first would
# scale the wrong workload and the trace UI would show no outage.
case "$POOL" in
  vllm-gpu|vllm-gpu-approved)
    candidates=("$POOL-predictor" "gpumoe-vllm" "cpureal-$POOL" "$POOL") ;;
  *)
    candidates=("$POOL-predictor" "cpureal-$POOL" "$POOL") ;;
esac
DEPLOY=""
for candidate in "${candidates[@]}"; do
  if oc -n "$NS" get "deploy/$candidate" >/dev/null 2>&1; then
    DEPLOY="$candidate"
    break
  fi
done
[ -n "$DEPLOY" ] || { echo "no deployment found for pool $POOL in $NS" >&2; exit 1; }

if $RESTORE; then
  echo "scaling $DEPLOY back to 1"
  oc -n "$NS" scale "deploy/$DEPLOY" --replicas=1
  oc -n "$NS" rollout status "deploy/$DEPLOY" --timeout=600s
else
  echo "scaling $DEPLOY to zero"
  oc -n "$NS" scale "deploy/$DEPLOY" --replicas=0
  echo "praxis health checks will mark it unhealthy within ~15s;"
  echo "watch: oc -n $NS port-forward svc/trace-ui 8080:8080  -> http://127.0.0.1:8080"
fi
