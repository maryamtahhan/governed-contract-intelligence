#!/usr/bin/env bash
# Apply the model pools, gateway, trace UI and network policies; requires 00
# and 01 to have succeeded. The trace-ui server is mounted from a ConfigMap
# created here from the Python package file, so no image build is needed.
# The root kustomization includes KServe objects; non-KServe stacks (ROSA)
# apply manifests/base plus their own pool overlay instead.
set -euo pipefail
cd "$(dirname "$0")/.."
NS="${NS:-openshell-demo}"

oc create configmap trace-ui-server \
  --from-file=server.py=demo/src/hybrid_demo/traceui/server.py \
  --from-file=otel.py=demo/src/hybrid_demo/traceui/otel.py \
  --dry-run=client -o yaml | oc apply -f - > /dev/null
oc apply -k manifests
