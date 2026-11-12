# ROSA policy smoke — 2026-11-12

Functional evidence only. This directory records the RC1 gateway policy smoke
on a live ROSA cluster; it is **not** a 200-document measurement and no
model-quality or cost number may be quoted from it.

- Image: `quay.io/mtahhan/gci-praxis:v0.9.2`
  (`sha256:3140a8dad2626ef71504546fa7901f53d9ec68ac49ed20e39919ae0af71c77c6`)
- Stack: `deploy/openshift/fake-all` on existing ROSA workers, with the
  cluster's storage class (`gp3-csi`); all five pools are simulated
- Command: `scripts/cluster/smoke.sh`
- Result: **27/27 checks passed** (`smoke.log`)

The smoke covers routing, fallback and recovery, sensitive-pool containment,
audit export, keyless/unknown-key denial, the key-to-step binding (classify
key claiming `risk` denied; matching key accepted), the general-entry PII
gate (PII 403, clean request served), and the step-less eval identity. The
last four checks read the gateway's own structured scanner/step-key records
and the trace UI's records.

The first run exposed a policy-configuration bug: `read_subject` projects
only the subject id, so the step-key gate saw an empty role set and denied
every keyed request with `step_key_mismatch`. The fix is to grant
`read_roles` to the step-key and audit plugins in all three policy documents;
it is now pinned by `demo/tests/test_policy_identity.py`. No image rebuild
was required. The live stack was reloaded from the corrected manifests and
the full smoke then passed.

The 2026-11-11 `hybrid` matrix remains the current quoted measurement
(`runs/rosa-200-2026-11-11-praxis-routes`). A real-pool rebuild and a fresh
200-document matrix were not run: the ROSA OCM login is expired and the
cluster has no GPU/x8i capacity. Do not create a measured `2026-11-12` row
from these fake pools.
