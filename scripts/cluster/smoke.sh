#!/usr/bin/env bash
# Assertion-based smoke test for the OpenShift stack — the cluster twin of
# scripts/linux/smoke.sh, with pool control (oc scale), logs (oc logs) and
# access (oc port-forward) instead of compose. Verifies the praxis
# work-to-pool table, the visible CPU->GPU fallback, strict fail-closed for
# sensitive judgment work, recovery, the governance claim from the trace UI's
# own records, and — since the PPE integration — the policy gate (keyless and
# unknown keys denied), the RC1 step-key and PII-gate denials, and the PII
# scanner's observe/gate lines.
#
# Run against the fake-all overlay (all five routes simulated, CPU-only
# cluster):
#   oc apply -k deploy/openshift/fake-all
#   scripts/cluster/smoke.sh
# The fake-all overlay runs a fake-specialist; the ROSA stack points the
# same route at the real RoBERTa service.
set -euo pipefail
cd "$(dirname "$0")/../.."
NS="${NS:-openshell-demo}"
GW="${GATEWAY:-http://127.0.0.1:18080}"
SGW="${SENSITIVE:-http://127.0.0.1:18083}"
# The gateway's PPE policy layer fails closed on unknown or missing API
# keys, and RC1 binds each key to its X-Demo-Step stage. The smoke script
# therefore presents the per-step key, not one shared eval key. Override any
# of these when a stack runs its own keys (digests in manifests/base/praxis/
# api-keys.yaml); the eval key is only for the step-less /v1/models probe and
# the stepless-eval exception.
KEY_CLASSIFY="${HYBRID_API_KEY_CLASSIFY:-gci-demo-key-classify}"
KEY_EXTRACT="${HYBRID_API_KEY_EXTRACT:-gci-demo-key-extract}"
KEY_RISK="${HYBRID_API_KEY_RISK:-gci-demo-key-risk}"
KEY_REPORT="${HYBRID_API_KEY_REPORT:-gci-demo-key-report}"
KEY_SCREEN="${HYBRID_API_KEY_SCREEN:-gci-demo-key-screen}"
EVAL_KEY="${HYBRID_API_KEY_EVAL:-gci-demo-key-eval}"
key_for_step() {
  case "${1%%:*}" in
    classify) printf '%s' "$KEY_CLASSIFY" ;;
    extract) printf '%s' "$KEY_EXTRACT" ;;
    risk) printf '%s' "$KEY_RISK" ;;
    report) printf '%s' "$KEY_REPORT" ;;
    screen|decide) printf '%s' "$KEY_SCREEN" ;;
    *) printf '%s' "$EVAL_KEY" ;;
  esac
}

PASS=0
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { PASS=$((PASS + 1)); echo "ok $PASS - $*"; }

pool_deployments() {
  case "$1" in
    cuad-specialist) echo fake-specialist ;;
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
  pool_start cuad-specialist 2>/dev/null || true
  pool_start vllm-cpu 2>/dev/null || true
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
for d in praxis trace-ui fake-specialist fake-vllm-cpu fake-vllm-gpu fake-vllm-gpu-approved fake-vllm-gpu-small; do
  oc -n "$NS" rollout status "deploy/$d" --timeout=300s >/dev/null \
    || fail "deployment $d not rolled out (apply -k deploy/openshift/fake-all?)"
done
restore_pools

# One port-forward carries every port the assertions touch: the two gateway
# entries and the five pool relays (health waits, direct warm-ups).
oc -n "$NS" port-forward svc/trace-ui \
  18080:8080 18081:8081 18082:8082 18083:8083 18084:8084 18085:8085 18088:8088 \
  >/dev/null 2>&1 &
PF_PID=$!
trap cleanup EXIT
sleep 5

wait_pools() {
  echo -n "waiting for model pools"
  for i in $(seq 1 120); do
    ok=0
    for port in 18088 18081 18082 18084 18085; do
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
  local key
  key=$(key_for_step "$2")
  curl -s -m 300 -o /dev/null "http://127.0.0.1:$1/v1/chat/completions" \
    -H 'Content-Type: application/json' -H "X-API-Key: $key" \
    -H "X-Demo-Step: $2" \
    -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"Say ok."}],"max_tokens":8}' \
    || true
}
echo -n "warming pools"
warm_pool 18088 extract:termination; echo -n " specialist"
warm_pool 18081 classify; echo -n " cpu"
warm_pool 18082 risk; echo -n " gpu"
warm_pool 18084 risk; echo -n " approved"
echo " done"

gateway_post() { # url step prompt max_tokens
  local key
  key=$(key_for_step "$2")
  # No -f: the fail-closed assertions below expect a non-2xx and inspect it.
  curl -s -D /tmp/h -o /tmp/b "$1/v1/chat/completions" \
    -H 'Content-Type: application/json' -H "X-API-Key: $key" \
    -H "X-Session-Id: smoke-test" -H "X-Demo-Step: $2" \
    -d "{\"model\":\"Semantic-Qwen\",\"messages\":[{\"role\":\"user\",\"content\":\"$3\"}],\"max_tokens\":$4}"
}
simple_prompt() { gateway_post "$GW" classify 'What is the capital of France?' 24; }
termination_prompt() { gateway_post "$GW" extract:termination 'Extract the termination clause from the following contract. If the clause is absent, say not found.' 30; }
data_protection_prompt() { gateway_post "$GW" extract:data_protection 'Extract the data protection clause from the following contract.' 30; }
risk_prompt() { gateway_post "$GW" risk 'Assess the aggregate risk exposure across these contracts and rank them.' 48; }
sensitive_simple() { gateway_post "$SGW" classify 'What is the capital of France? The data controller contact is aoife@example.com, EMP-123456.' 24; }
sensitive_risk() { gateway_post "$SGW" risk 'Assess the aggregate risk exposure across these contracts containing personal data; the data controller is aoife@example.com (EMP-123456).' 48; }
wrong_step_prompt() {
  curl -s -D /tmp/h -o /tmp/b "$GW/v1/chat/completions" \
    -H 'Content-Type: application/json' -H "X-API-Key: $KEY_CLASSIFY" \
    -H 'X-Session-Id: smoke-test' -H 'X-Demo-Step: risk' \
    -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":24}'
}
pii_gate_prompt() {
  curl -s -D /tmp/h -o /tmp/b "$GW/v1/chat/completions" \
    -H 'Content-Type: application/json' -H "X-API-Key: $KEY_CLASSIFY" \
    -H 'X-Session-Id: smoke-test' -H 'X-Demo-Step: classify' \
    -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"Contact aoife@example.com, EMP-123456 about the contract."}],"max_tokens":24}'
}
stepless_eval_prompt() {
  curl -s -D /tmp/h -o /tmp/b "$GW/v1/chat/completions" \
    -H 'Content-Type: application/json' -H "X-API-Key: $EVAL_KEY" \
    -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":24}'
}
keyless_prompt() { curl -s -D /tmp/h -o /tmp/b "$GW/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":24}'; }
badkey_prompt() { curl -s -D /tmp/h -o /tmp/b "$GW/v1/chat/completions" -H 'Content-Type: application/json' \
  -H 'X-API-Key: not-a-known-key' -H 'X-Session-Id: smoke-test' \
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":24}'; }
hdr() { tr -d '\r' < /tmp/h | grep -i "^$1:" | head -1 | cut -d' ' -f2- || true; }
status_of() { head -1 /tmp/h | tr -d '\r' | awk '{print $2}'; }

echo "== 1. gateway up"
curl -sf -H "X-API-Key: $EVAL_KEY" "$GW/v1/models" | grep -q Semantic-Qwen || fail "/v1/models"
ok "gateway serves /v1/models (keyed probe; keyless fails closed)"

echo "== 2. general entry: classify stays on the CPU pool"
simple_prompt
[ "$(status_of)" = 200 ] || fail "simple status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-cpu" ] || fail "classify route $(hdr X-Demo-Route)"
ok "general classify -> vllm-cpu"

echo "== 3. general entry: CUAD extraction routes to the specialist"
termination_prompt
[ "$(status_of)" = 200 ] || fail "termination status $(status_of)"
[ "$(hdr X-Demo-Route)" = "cuad-specialist" ] || fail "termination route $(hdr X-Demo-Route)"
ok "extract:termination -> cuad-specialist"

echo "== 4. general entry: data-protection extraction routes to the CPU MoE"
data_protection_prompt
[ "$(status_of)" = 200 ] || fail "data-protection status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-cpu" ] || fail "data-protection route $(hdr X-Demo-Route)"
ok "extract:data_protection -> vllm-cpu"

echo "== 5. general entry: judgment routes to the GPU pool"
risk_prompt
[ "$(status_of)" = 200 ] || fail "general risk status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-gpu" ] || fail "general risk route $(hdr X-Demo-Route)"
ok "general risk -> vllm-gpu"

echo "== 6. sensitive entry: CPU work stays approved"
sensitive_simple
[ "$(status_of)" = 200 ] || fail "sensitive simple status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-cpu" ] || fail "sensitive simple route $(hdr X-Demo-Route)"
ok "sensitive classify -> vllm-cpu"

echo "== 7. sensitive entry: judgment routes to the approved GPU pool"
sensitive_risk
[ "$(status_of)" = 200 ] || fail "sensitive risk status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-gpu-approved" ] || fail "sensitive risk route $(hdr X-Demo-Route)"
ok "sensitive risk -> vllm-gpu-approved"

echo "== 8. pull the plug on the CPU pool: general degrades visibly"
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
ok "general classify degraded to vllm-gpu (trace: failed CPU hop, fell-back badge)"
risk_prompt
[ "$(status_of)" = 200 ] || fail "risk during cpu outage"
[ "$(hdr X-Demo-Route)" = "vllm-gpu" ] || fail "risk route during cpu outage"
ok "risk traffic unaffected by the CPU outage"

echo "== 9. recovery"
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
ok "classify back on vllm-cpu, fallback cleared"

echo "== 10. kill the approved pool: sensitive judgment fails closed"
pool_stop vllm-gpu-approved
sleep 14
sensitive_risk
s_status=$(status_of)
if [ "$s_status" = 200 ]; then
  fail "sensitive risk served while the approved pool is down: route $(hdr X-Demo-Route)"
fi
route_during_fail=$(hdr X-Demo-Route)
if [ -n "${route_during_fail:-}" ] && [ "$route_during_fail" != "vllm-gpu-approved" ]; then
  fail "sensitive risk spilled to $route_during_fail"
fi
ok "sensitive risk failed closed (status $s_status, never spilled)"
risk_prompt
[ "$(status_of)" = 200 ] || fail "general risk broke while approved pool down"
[ "$(hdr X-Demo-Route)" = "vllm-gpu" ] || fail "general risk route during approved outage"
ok "general risk unaffected (pool isolation)"
sensitive_simple
[ "$(status_of)" = 200 ] || fail "sensitive simple broke while approved pool down"
[ "$(hdr X-Demo-Route)" = "vllm-cpu" ] || fail "sensitive simple route during approved outage"
ok "sensitive CPU work unaffected (approved pool only needed when CPU is down)"
pool_start vllm-gpu-approved
sleep 14

echo "== 11. governance: sensitive-entry traces never left the approved set"
(cd demo && uv run hybrid-score governance --trace-ui "$GW" > /tmp/gov.json)
python3 - <<'PY' || fail "governance check"
import json
gov = json.load(open("/tmp/gov.json"))
violations = gov.get("sensitive_entry_violations", [])
assert not violations, f"sensitive-entry violations: {violations}"
assert gov.get("sensitive_entry_traces", 0) > 0, "no sensitive-entry traces recorded"
print(f"    {gov['sensitive_entry_traces']} sensitive-entry traces, all on approved pools")
PY
ok "governance claim verified from the trace UI's own records"

echo "== 12. audit export"
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

echo "== 13. policy layer: the gateway fails closed without a valid key"
keyless_prompt
[ "$(status_of)" != "200" ] || fail "keyless request was served (status 200)"
ok "keyless request denied (status $(status_of))"
badkey_prompt
[ "$(status_of)" != "200" ] || fail "unknown key was served (status 200)"
ok "unknown key denied (status $(status_of))"
simple_prompt
[ "$(status_of)" = 200 ] || fail "known key not served after the denials: $(status_of)"
ok "known key still served"

echo "== 13b. policy layer: a valid key cannot claim another step"
wrong_step_prompt
[ "$(status_of)" != "200" ] || fail "classify key was served for X-Demo-Step: risk"
ok "classify key claiming risk denied (status $(status_of))"
sensitive_simple
[ "$(status_of)" = "200" ] || fail "sensitive entry rejected the correct classify key: $(status_of)"
ok "sensitive entry still accepts the matching step key"

echo "== 13c. policy layer: the general entry denies PII"
pii_gate_prompt
[ "$(status_of)" = "403" ] || fail "PII on the general entry: expected 403, got $(status_of)"
ok "PII-positive general request denied with 403"
simple_prompt
[ "$(status_of)" = "200" ] || fail "clean general request rejected after PII gate: $(status_of)"
ok "clean general request still served"

echo "== 13d. policy layer: the eval key may omit the step"
stepless_eval_prompt
[ "$(status_of)" = "200" ] || fail "step-less eval probe denied: $(status_of) $(cat /tmp/b)"
ok "step-less eval identity accepted (no review-stage claim)"

echo "== 14. policy layer: request records and the PII scanner"
praxis_logs > /tmp/praxis-logs.txt
records=$(grep -c 'gci-gateway' /tmp/praxis-logs.txt || true)
[ "${records:-0}" -gt 0 ] || fail "no audit records in praxis logs (source gci-gateway)"
ok "$records policy-layer request records emitted"
# The scanner line rides tracing, so its form depends on PRAXIS_LOG_FORMAT:
# plain text keeps the JSON verbatim; json mode escapes it inside message.
observe_markers=$(grep -cF 'gci_pii_observe' /tmp/praxis-logs.txt || true)
gate_markers=$(grep -cF 'gci_pii_gate' /tmp/praxis-logs.txt || true)
[ "${observe_markers:-0}" -gt 0 ] || fail "no pii-observe lines in praxis logs"
[ "${gate_markers:-0}" -gt 0 ] || fail "no pii-gate lines in praxis logs"
ok "$observe_markers observe + $gate_markers gate scanner lines emitted"
# The general-entry PII probe above must have produced a gate denial.
grep -qF 'deny' /tmp/praxis-logs.txt || fail "no PII gate denial recorded"
ok "PII gate recorded the general-entry denial"
# The sensitive prompts contain screen markers ("personal data", EMP-...),
# so at least one observe line must carry a match: the scanner sees content.
{ grep -qF '"matched":["' /tmp/praxis-logs.txt \
    || grep -qF '\"matched\":[\"' /tmp/praxis-logs.txt; } \
  || fail "no scanner line recorded a match"
ok "scanner observed PII content in the sensitive prompts"

echo
echo "ALL $PASS CHECKS PASSED"
echo "dashboard: oc -n $NS port-forward svc/trace-ui 8080:8080 -> http://127.0.0.1:8080"
