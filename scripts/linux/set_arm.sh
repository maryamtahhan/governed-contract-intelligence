#!/usr/bin/env bash
# Swap the live praxis config to an arm and wait until routing follows it.
# praxis watches the config file and hot-reloads, so this is a live flip: no
# container restart, no dropped trace database. Usage: set_arm A|B|C|D
set -euo pipefail
cd "$(dirname "$0")/../.."
STACK=deploy/linux
PRAXIS_DIR=manifests/praxis
COMPOSE="${COMPOSE:-docker compose}"
GATEWAY="${GATEWAY:-http://127.0.0.1:8080}"

arm_file() { case "$1" in A) echo arm-a.yaml;; B) echo arm-b.yaml;; C) echo arm-c.yaml;; *) echo config.yaml;; esac; }
expect_route() { case "$1" in A) echo vllm-gpu;; B) echo vllm-gpu-small;; D) echo vllm-cpu;; *) echo "";; esac; }

probe_route() {
  curl -s -m 60 -D /tmp/setarm-h -o /dev/null "$GATEWAY/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":16}'
  tr -d '\r' < /tmp/setarm-h | grep -i "^x-demo-route:" | head -1 | cut -d' ' -f2- || true
}

arm="${1:?usage: set_arm A|B|C|D}"
cp "$PRAXIS_DIR/$(arm_file "$arm")" "$PRAXIS_DIR/live.yaml"

expected=$(expect_route "$arm")
echo -n "praxis reloading to arm $arm"
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
echo "praxis did not pick up arm $arm (last probe route: $route)" >&2
exit 1
