#!/usr/bin/env bash
# Run the four routing policies (one-pool, small-first, round-robin, hybrid)
# on the same documents, then build the report. Each policy swaps the praxis
# config, waits for readiness, scrapes pool/praxis metrics before and after,
# and runs the review pipeline through the trace UI so every request is
# recorded. Device-time comes from the simulated pools' X-Busy-Ms headers.
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/configs.sh
STACK=deploy/linux
COMPOSE="${COMPOSE:-docker compose}"
RUNS="${RUNS:-runs}"
DOCS="${DOCS:-40}"
DATA="${DATA:-data/synthetic}"
CONCURRENCY="${CONCURRENCY:-12}"
GATEWAY="${GATEWAY:-http://127.0.0.1:8080}"
SENSITIVE="${SENSITIVE:-http://127.0.0.1:8083}"
CONFIGS="${CONFIGS:-${ARMS:-one-pool small-first round-robin hybrid}}"

for c in curl uv; do command -v "$c" >/dev/null || { echo "missing: $c" >&2; exit 1; }; done
$COMPOSE version >/dev/null 2>&1 || { echo "compose not usable: $COMPOSE" >&2; exit 1; }
curl -sf -H "X-API-Key: ${HYBRID_API_KEY_EVAL:-gci-demo-key-eval}" "$GATEWAY/v1/models" >/dev/null || { echo "stack not up: scripts/linux/up.sh" >&2; exit 1; }

if [ "$DATA" = "data/synthetic" ] && [ ! -f demo/data/synthetic/ground_truth.json ]; then
  echo "generating synthetic documents"
  (cd demo && uv run hybrid-gendata --out data/synthetic --count 200 --seed 42 --golden 20)
fi
if [ ! -f "demo/$DATA/ground_truth.json" ]; then
  echo "dataset not found: demo/$DATA" >&2; exit 1
fi

set_config() { bash scripts/linux/set_config.sh "$1" >/dev/null; }

scrape() {  # scrape <outdir> <tag>
  mkdir -p "$1"
  curl -sf http://127.0.0.1:19002/metrics > "$1/praxis-$2.txt" 2>/dev/null || true
  curl -sf http://127.0.0.1:18088/metrics > "$1/cuad-specialist-$2.txt" 2>/dev/null || true
  curl -sf http://127.0.0.1:18081/metrics > "$1/vllm-cpu-$2.txt" 2>/dev/null || true
  curl -sf http://127.0.0.1:18082/metrics > "$1/vllm-gpu-$2.txt" 2>/dev/null || true
  curl -sf http://127.0.0.1:18084/metrics > "$1/vllm-gpu-approved-$2.txt" 2>/dev/null || true
  curl -sf http://127.0.0.1:18085/metrics > "$1/vllm-gpu-small-$2.txt" 2>/dev/null || true
}

# Live metrics sampling during each run -> measured device-time in the report.
SAMPLE_ARGS=""
if curl -sf -m 3 http://127.0.0.1:18081/health >/dev/null 2>&1; then
  SAMPLE_ARGS="--sample-metrics cuad-specialist=http://127.0.0.1:18088,vllm-cpu=http://127.0.0.1:18081,vllm-gpu=http://127.0.0.1:18082,vllm-gpu-approved=http://127.0.0.1:18084,vllm-gpu-small=http://127.0.0.1:18085"
fi

for requested in $CONFIGS; do
  config=$(config_name "$requested")
  out="$RUNS/$config"
  mkdir -p "$out"
  echo
  echo "=== config $config ================================================"
  set_config "$config"
  scrape "$out" before
  (cd demo && uv run hybrid-run --target gateway \
    --gateway "$GATEWAY" --sensitive-gateway "$SENSITIVE" \
    --data "$DATA" --out "../$out" --config "$config" \
    --docs "$DOCS" --concurrency "$CONCURRENCY" $SAMPLE_ARGS)
  scrape "$out" after
done

echo
echo "=== report ========================================================="
repo_dir=$(pwd)
run_args=""
for requested in $CONFIGS; do
  run_args="$run_args $repo_dir/$RUNS/$(config_name "$requested")"
done
# shellcheck disable=SC2086
(cd demo && uv run hybrid-score report --runs $run_args \
  --data "$DATA" --out "$repo_dir/$RUNS/report.md")
echo
echo "report:    $RUNS/report.md (+ .json, .html)"
echo "configs table is also live on the trace UI: http://127.0.0.1:8080"
echo "audit pack: curl -s http://127.0.0.1:8080/_trace/api/audit.jsonl > $RUNS/audit.jsonl"
