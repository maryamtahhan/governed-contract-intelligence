#!/usr/bin/env bash
# Create the OpenClaw sandbox through the gateway with the openshell CLI; requires 00-02 to have succeeded.
# Port-forwards the gateway only while this script runs. Later `openshell` commands need their own
# port-forward: oc -n openshell-demo port-forward svc/openshell 8080:8080
set -euo pipefail
cd "$(dirname "$0")/.."
NS="${NS:-openshell-demo}"
NAME="${SANDBOX_NAME:-openclaw}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
PORT="${OPENCLAW_PORT:-18789}"
IMAGE="${OPENCLAW_IMAGE:-ghcr.io/openclaw/openclaw:latest}"
# The sandbox's egress policy allows trace-ui only. Do not default this to
# praxis: a direct gateway URL would bypass the ingress trace row.
BASE_URL="${SANDBOX_BASE_URL:-http://trace-ui.${NS}.svc.cluster.local:8080/v1}"
MODEL="${VLLM_MODEL_ID:-Semantic-Qwen}"   # name trace-ui forwards to Praxis /v1/models
# Praxis fails closed on unknown keys. The sandbox uses the eval identity
# because an agent turn is not one of the pipeline's four review steps; the
# same fixed demo credential is used by the smoke and model-comparison
# scripts. Override with SANDBOX_API_KEY when a stack runs its own keys.
API_KEY="${SANDBOX_API_KEY:-${HYBRID_API_KEY_EVAL:-gci-demo-key-eval}}"
# Keep OpenClaw inside the canonical 16k pool context. The full tool profile
# makes the agent prompt ~16.9k tokens before it even reads the message;
# `minimal` is enough for the demo agent and does not alter the measured
# model-pool configuration.
CONTEXT_WINDOW="${OPENCLAW_CONTEXT_WINDOW:-16384}"
MAX_TOKENS="${OPENCLAW_MAX_TOKENS:-512}"
command -v openshell >/dev/null || { echo "missing: openshell CLI (https://github.com/NVIDIA/OpenShell/releases)" >&2; exit 1; }

oc -n "$NS" port-forward svc/openshell "$LOCAL_PORT:8080" >/dev/null &
PF=$!
trap 'kill $PF 2>/dev/null' EXIT
for _ in $(seq 1 30); do (echo >"/dev/tcp/127.0.0.1/$LOCAL_PORT") 2>/dev/null && break; sleep 1; done

# http:// registers a plaintext gateway (the chart runs with server.disableTls=true). `gateway add` fails
# if the name exists, so drop any old registration first (it may point at a different local port).
openshell gateway remove "$NS" >/dev/null 2>&1 || true
openshell gateway add "http://127.0.0.1:$LOCAL_PORT" --name "$NS" >/dev/null

existing=$(openshell sandbox list | awk 'NR>1{print $1}')   # not piped into grep -q: pipefail + SIGPIPE
if grep -qx "$NAME" <<<"$existing"; then
  echo "sandbox '$NAME' already exists; leaving it as is (openshell sandbox delete $NAME to recreate; this deletes /sandbox)"
  exit 0
fi

openshell sandbox create --name "$NAME" --from "$IMAGE" --policy sandbox/policy.yaml --detach \
  --env VLLM_API_KEY="$API_KEY" --env TMPDIR=/tmp --env SQLITE_TMPDIR=/tmp \
  -- sh -c "openclaw onboard --non-interactive --accept-risk --skip-health --mode local \
--auth-choice vllm --custom-base-url '$BASE_URL' --custom-api-key '$API_KEY' \
--custom-model-id '$MODEL' && exec openclaw gateway --port $PORT --bind lan"

for _ in $(seq 1 100); do
  phase=$(openshell sandbox list | awk -v n="$NAME" '$1==n{print $NF}')
  if [ "$phase" = Ready ]; then
    # OpenClaw's provider config sends the OpenAI-style Authorization header
    # from apiKey; Praxis authenticates on X-API-Key. Add that header and
    # keep the local prompt inside the pool context. Config changes apply
    # without restarting the gateway.
    openshell sandbox exec "$NAME" -- openclaw config set tools.profile minimal >/dev/null
    openshell sandbox exec "$NAME" -- openclaw config set \
      models.providers.vllm.headers "{\"X-API-Key\":\"$API_KEY\"}" --strict-json >/dev/null
    openshell sandbox exec "$NAME" -- openclaw config set \
      'models.providers.vllm.models[0].contextWindow' "$CONTEXT_WINDOW" >/dev/null
    openshell sandbox exec "$NAME" -- openclaw config set \
      'models.providers.vllm.models[0].maxTokens' "$MAX_TOKENS" >/dev/null
    echo "sandbox '$NAME' ready"
    exit 0
  fi
  [ "$phase" = Error ] && { echo "sandbox '$NAME' is in Error: openshell sandbox get $NAME" >&2; exit 1; }
  sleep 6
done
echo "timed out waiting for '$NAME'; check: openshell sandbox get $NAME" >&2
exit 1
