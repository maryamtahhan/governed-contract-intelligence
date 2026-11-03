#!/usr/bin/env bash
# Check tools, cluster version, and install the Agent Sandbox CRDs/controller.
set -euo pipefail
AGENT_SANDBOX_VERSION="${AGENT_SANDBOX_VERSION:-v1.0.8}"
for c in oc helm; do command -v "$c" >/dev/null || { echo "missing: $c" >&2; exit 1; }; done
command -v openshell >/dev/null || echo "WARN: openshell CLI not found; install it before step 02 (see OpenShell README)."
oc whoami >/dev/null || { echo "not logged in: oc login ..." >&2; exit 1; }
echo "OpenShift: $(oc get clusterversion version -o jsonpath='{.status.desired.version}' 2>/dev/null || echo unknown)  (need >= 4.19 for Landlock)"
echo "CNI: $(oc get network.operator cluster -o jsonpath='{.spec.defaultNetwork.type}' 2>/dev/null || echo unknown)  (must enforce NetworkPolicy; OVN-Kubernetes does)"
if ! oc api-resources --api-group=agents.x-k8s.io 2>/dev/null | grep -q sandboxes; then
  echo "Installing Agent Sandbox ${AGENT_SANDBOX_VERSION} (cluster-admin required)"
  oc apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/sandbox.yaml"
else
  echo "Agent Sandbox CRDs already present"
fi
