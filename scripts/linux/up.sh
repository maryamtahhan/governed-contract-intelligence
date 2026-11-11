#!/usr/bin/env bash
# Build and start the local Linux stack (hybrid config by default).
# Works with docker compose or podman-compose:
#   COMPOSE="docker compose" ./scripts/linux/up.sh        (default)
#   COMPOSE="$HOME/pv/bin/podman-compose" ./scripts/linux/up.sh
# Add --real on a GPU instance to run real vLLM pools instead of the
# simulated backends (needs the NVIDIA driver, nvidia-container-toolkit with
# a CDI spec, and ~40GB of disk for models).
set -euo pipefail
cd "$(dirname "$0")/../.."
STACK=deploy/linux
COMPOSE="${COMPOSE:-docker compose}"
GATEWAY="${GATEWAY:-http://127.0.0.1:8080}"
PROFILE="fake"

for a in "$@"; do
  case "$a" in
    --real) PROFILE="real" ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

$COMPOSE version >/dev/null 2>&1 || { echo "compose not usable: $COMPOSE" >&2; exit 1; }
# live.yaml is the config praxis watches; arms are swapped by copying over it.
PRAXIS_DIR=manifests/base/praxis
[ -f "$PRAXIS_DIR/live.yaml" ] || cp "$PRAXIS_DIR/config.yaml" "$PRAXIS_DIR/live.yaml"

if [ "$PROFILE" = "real" ]; then
  # No RoBERTa service in the Linux compose profile: the specialist route
  # falls back to the real CPU model so the routing shape still works.
  export CUAD_SPECIALIST_UPSTREAM="http://vllm-cpu-real:8000"
  export VLLM_CPU_UPSTREAM="http://vllm-cpu-real:8000"
  export VLLM_GPU_UPSTREAM="http://vllm-gpu-real:8000"
  export VLLM_GPU_APPROVED_UPSTREAM="http://vllm-gpu-approved-real:8000"
  export VLLM_GPU_SMALL_UPSTREAM="http://vllm-gpu-small-real:8000"
  export JEV_CPU_UPSTREAM="http://jev-cpu-real:8000"
  echo "starting with REAL vLLM pools and the real Jev CPU screen (model downloads on first start)"
fi

$COMPOSE --profile "$PROFILE" -f "$STACK/docker-compose.yml" up -d --build

echo -n "waiting for the gateway"
ready=0
for i in $(seq 1 240); do
  if curl -sf -H "X-API-Key: ${HYBRID_API_KEY_EVAL:-gci-demo-key-eval}" "$GATEWAY/v1/models" >/dev/null 2>&1; then echo " ok"; ready=1; break; fi
  echo -n .
  sleep 2
done
if [ "$ready" != 1 ]; then
  echo
  echo "gateway did not come up: $COMPOSE -f $STACK/docker-compose.yml logs praxis trace-ui" >&2
  exit 1
fi
if [ "$PROFILE" = "real" ]; then
  echo -n "waiting for the model pools (first start downloads ~36GB of weights)"
  for i in $(seq 1 360); do
    ok=0
    for port in 18088 18081 18082 18084 18085; do
      curl -sf -m 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1 || ok=1
    done
    if [ "$ok" = 0 ]; then echo " ok"; break; fi
    echo -n .
    sleep 5
  done
fi
echo "trace UI:        http://127.0.0.1:8080"
echo "sensitive entry: http://127.0.0.1:8083 (pipeline use; same trace UI)"
echo "backends:        $PROFILE (pull the plug: stop the matching pool service)"
