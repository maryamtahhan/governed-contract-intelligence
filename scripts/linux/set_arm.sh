#!/usr/bin/env bash
# Swap the live praxis routing policy and wait until routing follows it.
# (Legacy filename; prefer set_config.sh. Both accept policy names.)
# praxis watches the config file and hot-reloads, so this is a live flip: no
# container restart, no dropped trace database.
# Usage: set_arm.sh all-gpu|gpu-small|round-robin|hybrid (legacy A-D accepted)
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/configs.sh
STACK=deploy/linux
PRAXIS_DIR=manifests/base/praxis
COMPOSE="${COMPOSE:-docker compose}"
GATEWAY="${GATEWAY:-http://127.0.0.1:8080}"

expect_route() {
  case "$(config_code "$1")" in
    A) echo vllm-gpu ;;
    B) echo vllm-gpu-small ;;
    D) echo vllm-cpu ;;
    *) echo "" ;;
  esac
}

probe_route() {
  curl -s -m 60 -D /tmp/setarm-h -o /dev/null "$GATEWAY/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":16}'
  tr -d '\r' < /tmp/setarm-h | grep -i "^x-demo-route:" | head -1 | cut -d' ' -f2- || true
}

requested="${1:?usage: set_arm.sh all-gpu|gpu-small|round-robin|hybrid}"
config=$(config_name "$requested")
cp "$PRAXIS_DIR/$(config_file "$requested")" "$PRAXIS_DIR/live.yaml"

expected=$(expect_route "$requested")
echo -n "praxis reloading to config $config"
for i in $(seq 1 30); do
  route=$(probe_route)
  if [ -n "$route" ]; then
    if [ -z "$expected" ] || [ "$route" = "$expected" ]; then
      echo " ok (probe route: $route)"
      exit 0
    fi
  fi
  echo -n .
  sleep 2
done
echo
echo "praxis did not pick up config $config (last probe route: $route)" >&2
exit 1
