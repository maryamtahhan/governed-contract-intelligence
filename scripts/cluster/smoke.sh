#!/usr/bin/env bash
# Assertion-based smoke test for the OpenShift stack — the cluster twin of
# scripts/linux/smoke.sh, same 18 assertions, with pool control (oc scale),
# logs (oc logs) and access (oc port-forward) instead of compose. Verifies
# the routing matrix, the visible fallback flag, strict fail-closed for
# sensitive REASONING work, recovery, the governance claim from the trace
# UI's own records, and — since the PPE integration — the policy gate
# (keyless and unknown keys denied) plus the shadow PII screen's observe
# lines.
#
# Run against the fake-all overlay (all pools simulated, CPU-only cluster):
#   oc apply -k deploy/openshift/fake-all
#   scripts/cluster/smoke.sh
# Pool names map to the fake-* Deployments; on a real-pool stack the
# deployment names differ (see deploy/openshift/rosa/pools.json).
set -euo pipefail
cd "$(dirname "$0")/../.."
NS="${NS:-openshell-demo}"
GW="${GATEWAY:-http://127.0.0.1:18080}"
SGW="${SENSITIVE:-http://127.0.0.1:18083}"
JGW="${JUDGMENT:-http://127.0.0.1:18086}"
SJGW="${SENSITIVE_JUDGMENT:-http://127.0.0.1:18087}"
# The gateway's PPE policy layer fails closed on unknown or missing API
# keys, so every prompt must present one. The eval identity is the smoke
# script's; override with HYBRID_API_KEY_EVAL when a stack runs its own
# keys (digests in manifests/base/praxis/api-keys.yaml).
API_KEY="${HYBRID_API_KEY_EVAL:-gci-demo-key-eval}"

PASS=0
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { PASS=$((PASS + 1)); echo "ok $PASS - $*"; }

pool_deployments() {
  case "$1" in
    vllm-cpu) echo fake-vllm-cpu ;;
    vllm-gpu) echo fake-vllm-gpu ;;
    vllm-gpu-approved) echo fake-vllm-gpu-approved ;;
    vllm-gpu-small) echo fake-vllm-gpu-small ;;
    *) echo "unknown pool: $1" >&2; return 1 ;;
  esac
}
pool_stop()  { oc -n "$NS" scale "deploy/$(pool_deployments "$1")" --replicas=0 >/dev/null; }
pool_start() { oc -n "$NS" scale "deploy/$(pool_deployments "$1")" --replicas=1 >/dev/null; }
praxis_logs() { oc -n "$NS" logs deploy/praxis 2>&1 || true; }

# A previously failed run may have left pools scaled to zero; bring them
# back and let praxis health checks mark them healthy before asserting.
restore_pools() {
  pool_start vllm-gpu 2>/dev/null || true
  pool_start vllm-gpu-approved 2>/dev/null || true
}
PF_PID=""
cleanup() {
  restore_pools
  [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true
}
trap cleanup EXIT

oc whoami >/dev/null || { echo "not logged in" >&2; exit 1; }
for d in praxis trace-ui fake-vllm-cpu fake-vllm-gpu fake-vllm-gpu-approved fake-vllm-gpu-small; do
  oc -n "$NS" rollout status "deploy/$d" --timeout=300s >/dev/null \
    || fail "deployment $d not rolled out (apply -k deploy/openshift/fake-all?)"
done
restore_pools

# One port-forward carries every port the assertions touch: the four
# gateway entries and the four pool relays (health waits, direct warm-ups).
oc -n "$NS" port-forward svc/trace-ui \
  18080:8080 18081:8081 18082:8082 18083:8083 18084:8084 18085:8085 18086:8086 18087:8087 \
  >/dev/null 2>&1 &
PF_PID=$!
trap cleanup EXIT
sleep 5

wait_pools() {
  echo -n "waiting for model pools"
  for i in $(seq 1 120); do
    ok=0
    for port in 18081 18082 18084 18085; do
      curl -sf -m 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1 || ok=1
    done
    if [ "$ok" = 0 ]; then echo " ok"; return 0; fi
    echo -n .
    sleep 5
  done
  echo; echo "pools did not become healthy" >&2; return 1
}
wait_pools

# Warm the pools through the gateway: the policy layer sits in the request
# path now, so warming it the way real traffic arrives also proves the gate
# passes keyed requests. Failures tolerated; the assertions below decide.
warm_pool() {
  curl -s -m 300 -o /dev/null "http://127.0.0.1:$1/v1/chat/completions" \
    -H 'Content-Type: application/json' -H "X-API-Key: $API_KEY" \
    -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"Say ok."}],"max_tokens":8}' \
    || true
}
echo -n "warming pools"
for port in 18081 18082 18084 18085; do
  warm_pool "$port"
  echo -n " $port"
done
echo " done"

simple_prompt() { curl -sf -D /tmp/h -o /tmp/b "$GW/v1/chat/completions" -H 'Content-Type: application/json' \
  -H "X-API-Key: $API_KEY" -H 'X-Session-Id: smoke-test' \
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":24}'; }
judgment_prompt() { curl -s -D /tmp/h -o /tmp/b "$JGW/v1/chat/completions" -H 'Content-Type: application/json' \
  -H "X-API-Key: $API_KEY" -H 'X-Session-Id: smoke-test' \
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"Assess the aggregate risk exposure across these contracts and rank them."}],"max_tokens":48}'; }
sensitive_judgment_prompt() { curl -s -D /tmp/h -o /tmp/b "$SJGW/v1/chat/completions" -H 'Content-Type: application/json' \
  -H "X-API-Key: $API_KEY" -H 'X-Session-Id: smoke-test' \
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"Assess the aggregate risk exposure across these contracts containing personal data and rank them."}],"max_tokens":48}'; }
sensitive_simple() { curl -sf -D /tmp/h -o /tmp/b "$SGW/v1/chat/completions" -H 'Content-Type: application/json' \
  -H "X-API-Key: $API_KEY" -H 'X-Session-Id: smoke-test' \
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France? The data controller contact is aoife@example.com, EMP-123456."}],"max_tokens":24}'; }
keyless_prompt() { curl -s -D /tmp/h -o /tmp/b "$GW/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":24}'; }
badkey_prompt() { curl -s -D /tmp/h -o /tmp/b "$GW/v1/chat/completions" -H 'Content-Type: application/json' \
  -H 'X-API-Key: not-a-known-key' -H 'X-Session-Id: smoke-test' \
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":24}'; }
hdr() { tr -d '\r' < /tmp/h | grep -i "^$1:" | head -1 | cut -d' ' -f2- || true; }
status_of() { head -1 /tmp/h | tr -d '\r' | awk '{print $2}'; }

echo "== 1. gateway up"
curl -sf -H "X-API-Key: $API_KEY" "$GW/v1/models" | grep -q Semantic-Qwen || fail "/v1/models"
ok "gateway serves /v1/models (keyed probe; keyless fails closed)"

echo "== 2. general entry: pinned to the CPU pool"
simple_prompt
[ "$(status_of)" = 200 ] || fail "simple status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-cpu" ] || fail "general route $(hdr X-Demo-Route)"
ok "general entry -> vllm-cpu (the pin; no classifier in the path)"

echo "== 3. judgment entry: pinned to the GPU pool, no CPU failover"
judgment_prompt
[ "$(status_of)" = 200 ] || fail "judgment status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-gpu" ] || fail "judgment route $(hdr X-Demo-Route)"
ok "judgment entry -> vllm-gpu"

echo "== 4. sensitive entry: stays on the in-cluster CPU pool"
sensitive_simple
[ "$(status_of)" = 200 ] || fail "sensitive simple status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-cpu" ] || fail "sensitive simple route $(hdr X-Demo-Route)"
ok "sensitive entry -> vllm-cpu"

echo "== 5. sensitive-judgment entry: pinned to the approved GPU pool"
sensitive_judgment_prompt
[ "$(status_of)" = 200 ] || fail "sensitive judgment status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-gpu-approved" ] || fail "sensitive judgment route $(hdr X-Demo-Route)"
ok "sensitive-judgment entry -> vllm-gpu-approved"

echo "== 6. pull the plug on the CPU pool: general degrades visibly"
pool_stop vllm-cpu
sleep 14
simple_prompt
[ "$(status_of)" = 200 ] || fail "general during cpu outage: $(status_of) $(cat /tmp/b)"
[ "$(hdr X-Demo-Route)" = "vllm-gpu" ] || fail "general during outage route $(hdr X-Demo-Route)"
# praxis does not emit X-Praxis-Fallback on proxied failovers (FINDINGS §7)
# and the label/route mismatch signal left with the classifier, so the
# durable degrade signal is the failed hop on a completed request — the
# trace UI badges it and this is what the run records show.
curl -s "$GW/_trace/api/traces" | python3 -c "
import json, sys
t = json.load(sys.stdin)['traces'][0]
assert t['route'] == 'vllm-gpu', t
assert t['fallback'] == 1, t
assert any(h.get('error') for h in t['hops']), t
" || fail "trace does not show the CPU->GPU fallback"
ok "general degraded to vllm-gpu (trace: failed CPU hop, fell-back badge)"
judgment_prompt
[ "$(status_of)" = 200 ] || fail "judgment during cpu outage"
[ "$(hdr X-Demo-Route)" = "vllm-gpu" ] || fail "judgment route during cpu outage"
ok "judgment traffic unaffected by the CPU outage (no CPU in its set)"

echo "== 7. recovery"
pool_start vllm-cpu
echo -n "waiting for the CPU pool to reload"
for i in $(seq 1 90); do
  curl -sf -m 3 http://127.0.0.1:18081/health >/dev/null 2>&1 && { echo " up"; break; }
  echo -n .
  sleep 5
done
# praxis needs two consecutive successful probes before routing back.
sleep 12
simple_prompt
[ "$(status_of)" = 200 ] || fail "general after recovery: $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-cpu" ] || fail "general after recovery route $(hdr X-Demo-Route)"
curl -s "$GW/_trace/api/traces" | python3 -c "
import json, sys
t = json.load(sys.stdin)['traces'][0]
assert t['route'] == 'vllm-cpu', t
assert t['fallback'] == 0, t
assert not any(h.get('error') for h in t['hops']), t
" || fail "trace still shows fallback after recovery"
ok "general back on vllm-cpu, fallback cleared"

echo "== 8. kill the approved pool: sensitive-judgment fails closed"
pool_stop vllm-gpu-approved
sleep 14
sensitive_judgment_prompt
s_status=$(status_of)
if [ "$s_status" = 200 ]; then
  fail "sensitive judgment served while the approved pool is down: route $(hdr X-Demo-Route)"
fi
route_during_fail=$(hdr X-Demo-Route)
if [ -n "${route_during_fail:-}" ] && [ "$route_during_fail" != "vllm-gpu-approved" ]; then
  fail "sensitive judgment spilled to $route_during_fail"
fi
ok "sensitive-judgment failed closed (status $s_status, never spilled)"
judgment_prompt
[ "$(status_of)" = 200 ] || fail "judgment broke while approved pool down"
[ "$(hdr X-Demo-Route)" = "vllm-gpu" ] || fail "judgment route during approved outage"
ok "judgment unaffected (pool isolation)"
sensitive_simple
[ "$(status_of)" = 200 ] || fail "sensitive simple broke while approved pool down"
[ "$(hdr X-Demo-Route)" = "vllm-cpu" ] || fail "sensitive simple route during approved outage"
ok "sensitive entry unaffected (approved pool only needed when CPU is down)"
pool_start vllm-gpu-approved
sleep 14

echo "== 9. governance: sensitive-entry traces never left the approved set"
(cd demo && uv run hybrid-score governance --trace-ui "$GW" > /tmp/gov.json)
python3 - <<'EOF' || fail "governance check"
import json
gov = json.load(open("/tmp/gov.json"))
violations = gov.get("sensitive_entry_violations", [])
assert not violations, f"sensitive-entry violations: {violations}"
assert gov.get("sensitive_entry_traces", 0) > 0, "no sensitive-entry traces recorded"
print(f"    {gov['sensitive_entry_traces']} sensitive-entry traces, all on approved pools")
EOF
ok "governance claim verified from the trace UI's own records"

echo "== 10. audit export"
curl -sf "$GW/_trace/api/audit.jsonl" -o /tmp/audit.jsonl || fail "audit.jsonl fetch"
[ -s /tmp/audit.jsonl ] || fail "audit.jsonl empty"
python3 -c "
import json
lines = [line for line in open('/tmp/audit.jsonl') if line.strip()]
assert lines, 'no traces exported'
for line in lines:
    json.loads(line)
print(f'    {len(lines)} traces exported as JSONL')
" || fail "audit.jsonl parse"
ok "audit.jsonl exports one JSON object per trace"

echo "== 11. policy layer: the gateway fails closed without a valid key"
keyless_prompt
[ "$(status_of)" != "200" ] || fail "keyless request was served (status 200)"
ok "keyless request denied (status $(status_of))"
badkey_prompt
[ "$(status_of)" != "200" ] || fail "unknown key was served (status 200)"
ok "unknown key denied (status $(status_of))"
simple_prompt
[ "$(status_of)" = 200 ] || fail "known key not served after the denials: $(status_of)"
ok "known key still served"

echo "== 12. policy layer: request records and the shadow PII screen"
praxis_logs > /tmp/praxis-logs.txt
records=$(grep -c 'gci-gateway' /tmp/praxis-logs.txt || true)
[ "${records:-0}" -gt 0 ] || fail "no audit records in praxis logs (source gci-gateway)"
ok "$records policy-layer request records emitted"
# The observe line rides tracing, so its form depends on PRAXIS_LOG_FORMAT:
# plain text keeps the JSON verbatim; json mode escapes it inside message.
observe_markers=0
grep -cF '"plugin":"gci_pii_observe"' /tmp/praxis-logs.txt >/dev/null 2>&1 \
  && observe_markers=$(grep -cF '"plugin":"gci_pii_observe"' /tmp/praxis-logs.txt || true)
grep -cF '\\"plugin\\":\\"gci_pii_observe\\"' /tmp/praxis-logs.txt >/dev/null 2>&1 \
  && observe_markers=$((observe_markers + $(grep -cF '\\"plugin\\":\\"gci_pii_observe\\"' /tmp/praxis-logs.txt || true)))
[ "${observe_markers:-0}" -gt 0 ] || fail "no pii-observe lines in praxis logs"
ok "$observe_markers shadow-PII observe lines emitted (scanner fires on cmf.llm_input)"
# The sensitive prompts contain screen markers ("personal data", EMP-...),
# so at least one observe line must carry a match: the scanner sees content.
{ grep -qF '"matched":["' /tmp/praxis-logs.txt \
    || grep -qF '\\"matched\\":[\\"' /tmp/praxis-logs.txt; } \
  || fail "no observe line recorded a match"
ok "scanner observed PII content in the sensitive prompts"

echo
echo "ALL $PASS CHECKS PASSED"
echo "dashboard: oc -n $NS port-forward svc/trace-ui 8080:8080 -> http://127.0.0.1:8080"
