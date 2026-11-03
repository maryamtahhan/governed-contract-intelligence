#!/usr/bin/env bash
# Assertion-based smoke test for the local Linux stack. Verifies the routing
# matrix, the visible fallback flag, strict fail-closed for sensitive
# REASONING work, recovery, and the governance claim from the trace UI's own
# records. Run this on the Linux instance after scripts/linux/up.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
STACK=deploy/linux
COMPOSE="${COMPOSE:-docker compose}"
GW="${GATEWAY:-http://127.0.0.1:8080}"
SGW="${SENSITIVE:-http://127.0.0.1:8083}"

PASS=0
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { PASS=$((PASS + 1)); echo "ok $PASS - $*"; }

# podman-compose stop/start is slow (it re-resolves the whole project); when
# running under podman, talk to the containers directly. Pool services differ
# per profile (fake-* vs *-real); try both, ignore the one that does not exist.
pool_services() {
  case "$1" in
    vllm-cpu) echo fake-cpu vllm-cpu-real ;;
    vllm-gpu) echo fake-gpu vllm-gpu-real ;;
    vllm-gpu-approved) echo fake-approved vllm-gpu-approved-real ;;
    vllm-gpu-small) echo fake-gpu-small vllm-gpu-small-real ;;
  esac
}
pool_stop() {
  for svc in $(pool_services "$1"); do
    case "$COMPOSE" in
      *podman*) podman stop "hybrid-demo_${svc}_1" >/dev/null 2>&1 || true ;;
      *) dc stop "$svc" >/dev/null 2>&1 || true ;;
    esac
  done
}
pool_start() {
  for svc in $(pool_services "$1"); do
    case "$COMPOSE" in
      *podman*) podman start "hybrid-demo_${svc}_1" >/dev/null 2>&1 || true ;;
      *) dc start "$svc" >/dev/null 2>&1 || true ;;
    esac
  done
}

dc() { $COMPOSE -f "$STACK/docker-compose.yml" "$@" >/dev/null; }

# A previously failed run may have left pools stopped; bring them back and
# let praxis health checks mark them healthy before asserting anything.
restore_pools() {
  pool_start vllm-gpu 2>/dev/null || true
  pool_start vllm-gpu-approved 2>/dev/null || true
}
trap restore_pools EXIT
restore_pools
sleep 14

wait_pools() {
  echo -n "waiting for model pools"
  for i in $(seq 1 240); do
    ok=0
    for port in 18081 18082 18084 18085; do
      curl -sf -m 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1 || ok=1
    done
    if [ "$ok" = 0 ]; then echo " ok"; return 0; fi
    echo -n .
    sleep 5
  done
  echo; echo "pools did not become healthy (model download?)" >&2; return 1
}
wait_pools

# Warm the pools: the first real inference can take a minute (torch.compile on
# the CPU backend, CUDA-graph capture on GPU) which would otherwise eat into
# praxis's step timeout. Fire one request per pool, failures tolerated.
warm_pool() {
  curl -s -m 300 -o /dev/null "http://127.0.0.1:$1/v1/chat/completions" \
    -H 'Content-Type: application/json' \
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
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":24}'; }
reasoning_prompt() { curl -s -D /tmp/h -o /tmp/b "$GW/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"Prove that the halting problem is undecidable, then compare it with the halting problem across all 3 documents and synthesise the overall risk report."}],"max_tokens":48}'; }
sensitive_reasoning() { curl -s -D /tmp/h -o /tmp/b "$SGW/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"Produce the cross-document risk report across all 3 documents containing personal data for the data controller. Synthesise and compare the extractions, derive the aggregate counts, and prove the ranking. Return JSON."}],"max_tokens":48}'; }
sensitive_simple() { curl -sf -D /tmp/h -o /tmp/b "$SGW/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"Semantic-Qwen","messages":[{"role":"user","content":"What is the capital of France? The data controller contact is aoife@example.com, EMP-123456."}],"max_tokens":24}'; }
hdr() { tr -d '\r' < /tmp/h | grep -i "^$1:" | head -1 | cut -d' ' -f2- || true; }
status_of() { head -1 /tmp/h | tr -d '\r' | awk '{print $2}'; }

echo "== 1. gateway up"
curl -sf "$GW/v1/models" | grep -q Semantic-Qwen || fail "/v1/models"
ok "gateway serves /v1/models"

echo "== 2. SIMPLE routes to the CPU pool"
simple_prompt
[ "$(status_of)" = 200 ] || fail "simple status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-cpu" ] || fail "simple route $(hdr X-Demo-Route)"
ok "SIMPLE -> vllm-cpu (label $(hdr X-Demo-Classification))"

echo "== 3. REASONING routes to the GPU pool"
reasoning_prompt
[ "$(status_of)" = 200 ] || fail "reasoning status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-gpu" ] || fail "reasoning route $(hdr X-Demo-Route)"
ok "REASONING -> vllm-gpu (label $(hdr X-Demo-Classification))"

echo "== 4. sensitive entry: SIMPLE stays on the in-cluster CPU pool"
sensitive_simple
[ "$(status_of)" = 200 ] || fail "sensitive simple status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-cpu" ] || fail "sensitive simple route $(hdr X-Demo-Route)"
ok "SENSITIVE+SIMPLE -> vllm-cpu"

echo "== 5. sensitive entry: REASONING pinned to the approved GPU pool"
sensitive_reasoning
[ "$(status_of)" = 200 ] || fail "sensitive reasoning status $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-gpu-approved" ] || fail "sensitive reasoning route $(hdr X-Demo-Route)"
ok "SENSITIVE+REASONING -> vllm-gpu-approved"

echo "== 6. pull the plug on the GPU pool"
pool_stop vllm-gpu
sleep 14
reasoning_prompt
[ "$(status_of)" = 200 ] || fail "reasoning during gpu outage: $(status_of) $(cat /tmp/b)"
[ "$(hdr X-Demo-Route)" = "vllm-cpu" ] || fail "reasoning during outage route $(hdr X-Demo-Route)"
# praxis starts the failover step with empty headers, so the degraded response
# carries no classifier label; the trace records the original label on the
# failed GPU hop and badges the trace as "fell back". Verify from the trace:
curl -s "$GW/_trace/api/traces" | python3 -c "
import json, sys
t = json.load(sys.stdin)['traces'][0]
assert t['classification'] == 'REASONING', t
assert t['route'] == 'vllm-cpu', t
assert t['fallback'] == 1, t
assert any(h.get('error') for h in t['hops']), t
" || fail "trace does not show the REASONING->CPU fallback"
ok "REASONING degraded to CPU (trace: failed GPU hop, fell-back badge)"
simple_prompt
[ "$(status_of)" = 200 ] || fail "simple during outage"
ok "SIMPLE traffic unaffected by the GPU outage"

echo "== 7. recovery"
pool_start vllm-gpu
echo -n "waiting for the GPU pool to reload"
for i in $(seq 1 90); do
  curl -sf -m 3 http://127.0.0.1:18082/health >/dev/null 2>&1 && { echo " up"; break; }
  echo -n .
  sleep 5
done
# praxis needs two consecutive successful probes before routing back.
sleep 12
reasoning_prompt
[ "$(status_of)" = 200 ] || fail "reasoning after recovery: $(status_of)"
[ "$(hdr X-Demo-Route)" = "vllm-gpu" ] || fail "reasoning after recovery route $(hdr X-Demo-Route)"
[ "$(hdr X-Demo-Classification)" = "REASONING" ] || fail "label after recovery $(hdr X-Demo-Classification)"
ok "REASONING back on vllm-gpu, mismatch cleared"

echo "== 8. kill the approved pool: sensitive REASONING fails closed"
pool_stop vllm-gpu-approved
sleep 14
sensitive_reasoning
s_status=$(status_of)
if [ "$s_status" = 200 ]; then
  fail "sensitive reasoning served while the approved pool is down: route $(hdr X-Demo-Route)"
fi
route_during_fail=$(hdr X-Demo-Route)
if [ -n "${route_during_fail:-}" ] && [ "$route_during_fail" != "vllm-gpu-approved" ]; then
  fail "sensitive reasoning spilled to $route_during_fail"
fi
ok "SENSITIVE+REASONING failed closed (status $s_status, never spilled)"
reasoning_prompt
[ "$(status_of)" = 200 ] || fail "general reasoning broke while approved pool down"
[ "$(hdr X-Demo-Route)" = "vllm-gpu" ] || fail "general reasoning route during approved outage"
ok "general REASONING unaffected (pool isolation)"
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

echo
echo "ALL $PASS CHECKS PASSED"
echo "dashboard: $GW  (traces, pool health, arms table)"
