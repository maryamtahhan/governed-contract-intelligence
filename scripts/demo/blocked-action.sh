#!/usr/bin/env bash
# The "agent is blocked" moment: attempt egress from the OpenClaw sandbox that
# the policy denies, and one it allows, to show default-deny in action.
# Requires the sandbox from scripts/03-create-sandbox.sh and a port-forward:
#   oc -n openshell-demo port-forward svc/openshell 8080:8080
set -euo pipefail
NAME="${SANDBOX_NAME:-openclaw}"

echo "== policy (sandbox/policy.yaml): egress is trace-ui ingress (read-write)"
echo "==   plus read-only api.github.com for curl; Praxis, pools and everything else: denied"
echo

echo "-- 1. unlisted host (https://example.com) - DENIED"
openshell sandbox exec "$NAME" -- sh -c 'curl -sS -m 8 https://example.com >/dev/null && echo UNEXPECTEDLY-ALLOWED || echo "denied (as expected)"' || true

echo "-- 2. write request to api.github.com (POST) - DENIED (read-only rule)"
openshell sandbox exec "$NAME" -- sh -c 'curl -sS -m 8 -X POST https://api.github.com -o /dev/null -w "%{http_code}\n" || echo "denied (as expected)"' || true

echo "-- 3. read request to api.github.com (GET) - ALLOWED"
openshell sandbox exec "$NAME" -- sh -c 'curl -sS -m 8 -o /dev/null -w "%{http_code}\n" https://api.github.com/zen' || true

echo "-- 4. trace-ui general ingress health - ALLOWED"
openshell sandbox exec "$NAME" -- sh -c 'curl -sS -m 8 -o /dev/null -w "%{http_code}\n" http://trace-ui.openshell-demo.svc.cluster.local:8080/_trace/healthz' || true

echo "-- 5. direct Praxis call - DENIED"
openshell sandbox exec "$NAME" -- sh -c 'curl -sS -m 8 -o /dev/null -w "%{http_code}\n" http://praxis.openshell-demo.svc.cluster.local:80/healthy' || true

echo
echo "Full egress evidence: openshell logs $NAME   (every allow and denial)"
