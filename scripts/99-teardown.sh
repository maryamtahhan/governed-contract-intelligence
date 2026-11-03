#!/usr/bin/env bash
# Tear down the demo, in the order that leaves nothing behind
set -euo pipefail
cd "$(dirname "$0")/.."

NS="${NS:-openshell-demo}"
RELEASE="${RELEASE:-openshell}"
AGENT_SANDBOX_VERSION="${AGENT_SANDBOX_VERSION:-v1.0.8}"
YES=false; DRY=false; DEL_NS=false; DEL_AS=false
for a in "$@"; do
  case "$a" in
    --yes) YES=true ;; --dry-run) DRY=true ;;
    --delete-namespace) DEL_NS=true ;; --agent-sandbox) DEL_AS=true ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

command -v oc >/dev/null || { echo "missing: oc" >&2; exit 1; }
oc whoami >/dev/null 2>&1 || { echo "not logged in: oc login ..." >&2; exit 1; }
oc get ns "$NS" >/dev/null 2>&1 || { echo "namespace $NS not found; nothing to do for namespaced resources"; DEL_NS=false; }

run() { echo "+ $*"; $DRY || "$@"; }
step() { echo; echo "== $*"; }

echo "Cluster:   $(oc whoami --show-server)"
echo "Namespace: $NS   Helm release: $RELEASE"
echo "Will delete: manifests, sandboxes + their objects, the gateway release and its data PVC$($DEL_NS && echo ', the namespace')$($DEL_AS && echo ', the cluster-wide Agent Sandbox controller + CRD')."
if ! $YES && ! $DRY; then
  read -r -p "Continue? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
fi

step "1/4 manifests"
run oc delete -k manifests --ignore-not-found --wait=true

step "2/4 sandboxes created by the gateway"
# The Sandbox CR owns its workload pod, supervisor pod and workspace PVC; garbage collection removes them.
run oc -n "$NS" delete sandbox -l openshell.ai/managed-by=openshell --ignore-not-found --wait=true --timeout=180s
# Anything the label still matches (boundary Service, per-sandbox Secrets, stragglers).
run oc -n "$NS" delete pod,svc,secret -l openshell.ai/managed-by=openshell --ignore-not-found --wait=true --timeout=120s

step "3/4 OpenShell gateway"
if command -v helm >/dev/null && helm -n "$NS" status "$RELEASE" >/dev/null 2>&1; then
  run helm uninstall "$RELEASE" -n "$NS" --wait
elif command -v helm >/dev/null; then
  echo "no Helm release '$RELEASE' in $NS; skipping"
else
  echo "WARN: helm not found; uninstall the gateway yourself: helm uninstall $RELEASE -n $NS" >&2
fi

step "4/4 gateway data PVC"
pvcs=$(oc -n "$NS" get pvc -o name 2>/dev/null | grep -E '^persistentvolumeclaim/openshell-data-' || true)
if [ -n "$pvcs" ]; then
  # shellcheck disable=SC2086
  run oc -n "$NS" delete $pvcs --ignore-not-found --wait=true
else
  echo "no gateway PVC found"
fi

if $DEL_NS; then step "namespace"; run oc delete ns "$NS" --ignore-not-found --wait=true; fi
if $DEL_AS; then
  step "Agent Sandbox controller + CRD (cluster-wide)"
  run oc delete -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/sandbox.yaml" --ignore-not-found --wait=true
fi

echo; echo "Done.$($DRY && echo ' (dry run: nothing was deleted)')"
