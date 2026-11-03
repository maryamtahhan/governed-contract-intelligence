#!/usr/bin/env bash
# Fetch CUAD v1 (Contract Understanding Atticus Dataset) for the real-contract
# run. ~510 commercial contracts from SEC EDGAR with expert clause annotations,
# released under CC BY 4.0 - VERIFY the licence and counts before use (V12),
# and never mix real customer data into the demo.
set -euo pipefail
cd "$(dirname "$0")/../.."
DEST="${DEST:-demo/data/cuad}"

if [ -d "$DEST/CUAD_v1" ]; then
  echo "CUAD already present: $DEST/CUAD_v1"
  exit 0
fi
mkdir -p "$DEST"
if [ ! -d "$DEST/cuad" ]; then
  echo "downloading CUAD (tarball; ~20MB)"
  curl -sfL https://github.com/The-Atticus-Project/cuad/archive/refs/heads/main.tar.gz \
    | tar -xzf - -C "$DEST"
  mv "$DEST/cuad-main" "$DEST/cuad"
fi
echo "converted next with:"
echo "  (cd demo && uv run python -m hybrid_demo.pipeline.cuad --cuad-dir ../$DEST --out data/cuad-docs --count 200)"
