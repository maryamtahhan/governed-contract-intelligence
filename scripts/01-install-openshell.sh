#!/usr/bin/env bash
# Install the OpenShell gateway chart with the OpenShift SCC overlay + our override.
set -euo pipefail
cd "$(dirname "$0")/.."
NS="${NS:-openshell-demo}"
VERSION="${OPENSHELL_VERSION:-0.1.6}"   # chart tags track releases; pin it
CHART="oci://ghcr.io/nvidia/openshell/helm-chart"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
# The SCC overlay ships in the chart repo's ci/ dir, not in the packaged chart.
curl -fsSL "https://raw.githubusercontent.com/NVIDIA/OpenShell/v${VERSION}/deploy/helm/openshell/ci/values-openshift-scc.yaml" -o "$WORK/scc.yaml" \
  || { echo "no v${VERSION} tag for scc overlay; using main"; curl -fsSL https://raw.githubusercontent.com/NVIDIA/OpenShell/main/deploy/helm/openshell/ci/values-openshift-scc.yaml -o "$WORK/scc.yaml"; }
oc get ns "$NS" >/dev/null 2>&1 || oc create ns "$NS"
helm upgrade --install openshell "$CHART" --version "$VERSION" -n "$NS" \
  -f "$WORK/scc.yaml" -f values/openshift-override.yaml
oc -n "$NS" rollout status statefulset/openshell --timeout=300s
