#!/usr/bin/env bash
# Choreographed on-stage demo on the local Linux stack. Each act pauses for
# narration; keep the trace UI (http://127.0.0.1:8080) on screen.
set -euo pipefail
cd "$(dirname "$0")/../.."
STACK=deploy/linux
COMPOSE="${COMPOSE:-docker compose}"
RUNS="${RUNS:-runs}"
GW="${GATEWAY:-http://127.0.0.1:8080}"
SGW="${SENSITIVE:-http://127.0.0.1:8083}"
DOCS="${DOCS:-40}"

dc() { $COMPOSE -f "$STACK/docker-compose.yml" "$@" >/dev/null; }
pool_services() {
  case "$1" in
    cuad-specialist) echo fake-specialist ;;
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
pause() { echo; read -r -p "── $* (Enter to continue) " _; }
set_config() { bash scripts/linux/set_config.sh "$1"; }
counts() { curl -sf "$GW/_trace/api/summary" | python3 -c "
import json, sys
s = json.load(sys.stdin)
total = s.get('total', 0)
fb = sum(g['count'] for g in s.get('groups', []) if g.get('fallback'))
sens = sum(g['count'] for g in s.get('groups', []) if (g.get('entry') or '').startswith('sensitive'))
print(f'traces={total} fallback={fb} sensitive-entry={sens}')"; }

[ -f demo/data/synthetic/ground_truth.json ] || (cd demo && uv run hybrid-gendata --out data/synthetic --count 200 --seed 42 --golden 20)
curl -sf -H "X-API-Key: ${HYBRID_API_KEY_EVAL:-gci-demo-key-eval}" "$GW/v1/models" >/dev/null || { echo "stack not up: scripts/linux/up.sh" >&2; exit 1; }

echo "On screen: $GW  (architecture diagram + live request traces)"
echo "The story: a legal-ops batch of $DOCS contracts; praxis routes by step."
echo "CUAD extraction goes to the specialist, data-protection/classify to the"
echo "CPU MoE, and risk/report to the GPU route for the entry (general vllm-gpu,"
echo "sensitive vllm-gpu-approved). Personal-data documents stay on approved pools."

pause "ACT 1 - fan-out: $DOCS contracts through the hybrid config"
set_config hybrid
(cd demo && uv run hybrid-run --target gateway --gateway "$GW" --sensitive-gateway "$SGW" \
  --data data/synthetic --out "../$RUNS/demo-hybrid" --config hybrid --docs "$DOCS" --concurrency 12 >/dev/null) &
RUNPID=$!
echo "batch running (pid $RUNPID); watch the traces table and the diagram light up"
echo "live: $(counts)"
sleep 8; echo "live: $(counts)"
wait $RUNPID
echo "batch done: $(counts)"
echo "Split to point at: CUAD rows on cuad-specialist, data-protection/classify"
echo "rows on vllm-cpu, risk/report rows on a GPU pool, and SENS-badged rows"
echo "staying on approved pools."

pause "ACT 2 - sensitivity pinning, verified from the gateway's own records"
(cd demo && uv run hybrid-score governance --trace-ui "$GW")

pause "ACT 3 - pull the plug: GPU pool removed mid-run"
(cd demo && uv run hybrid-run --target gateway --gateway "$GW" --sensitive-gateway "$SGW" \
  --data data/synthetic --out "../$RUNS/demo-hybrid-outage" --config hybrid --docs "$DOCS" --concurrency 12 >/dev/null) &
RUNPID=$!
sleep 6
echo "scaling the GPU pool to zero..."
pool_stop vllm-gpu
for _ in 1 2 3; do sleep 5; echo "during outage: $(counts)"; done
echo "Non-sensitive judgment now fails closed on the GPU route; CPU work"
echo "continues. The general entry's CPU->GPU fallback is in smoke.sh."
pool_start vllm-gpu
wait $RUNPID
echo "GPU back; recovered: $(counts)"

pause "ACT 4 - fail closed: the approved pool is removed"
pool_stop vllm-gpu-approved
sleep 12
(cd demo && uv run hybrid-run --target gateway --gateway "$GW" --sensitive-gateway "$SGW" \
  --data data/synthetic --out "../$RUNS/demo-hybrid-failclosed" --config hybrid --docs 8 --concurrency 4 >/dev/null) || true
echo "Sensitive judgment and the report failed closed (explicit error), never"
echo "spilled to a non-approved pool; extraction continued. Traces show the errors."
pool_start vllm-gpu-approved
sleep 12

pause "ACT 5 - kill switch: route everything to GPU (one-pool config) and compare"
set_config one-pool
(cd demo && uv run hybrid-run --target gateway --gateway "$GW" --sensitive-gateway "$SGW" \
  --data data/synthetic --out "../$RUNS/demo-one-pool" --config one-pool --docs "$DOCS" --concurrency 12 >/dev/null)
echo "one-pool config done: $(counts)"
echo "Note the GPU-seconds per document difference in the configs table (simulated"
echo "device-time on this stack; real pools measure the same way from /metrics)."

set_config hybrid
echo
echo "Routing configs table: $GW (bottom section)"
echo "Full report: scripts/linux/arms.sh then see $RUNS/report.md"
