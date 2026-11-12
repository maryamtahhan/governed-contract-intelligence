# Real ROSA RC1 gate assertions — 2026-11-12

Live `v0.9.2` gateway on real pools, immediately after the two-document
smoke and before the 200-document matrix. Captured in `gate.log`.

| check | status | result |
|---|---:|---|
| keyed `/v1/models` | 200 | — |
| keyless general request | 401 | `auth.missing_credential` |
| unknown key | 401 | `auth.key_unknown` |
| classify key claiming `risk` | 403 | `step_key_mismatch` |
| PII on general entry | 403 | `pii_detected` |
| clean general classify | 200 | route `vllm-cpu` |
| sensitive PII classify | 200 | route `vllm-cpu` |
| sensitive risk | 200 | route `vllm-gpu-approved` |
| step-less eval | 200 | route `vllm-cpu` |
| audit export | — | 33 JSON records parsed |

The general PII probe used an email plus employee id; the response body named
`EMAIL_ADDRESS` and `EMPLOYEE_ID`. The sensitive probe carried the same PII
on the `:8083` entry and was served, proving the observe-only boundary.

This is the real-pool counterpart to the 27/27 fake-all smoke in
`runs/rosa-policy-2026-11-12/`. Both ran the published `v0.9.2` digest
`sha256:3140a8dad2626ef71504546fa7901f53d9ec68ac49ed20e39919ae0af71c77c6`;
this directory is the one that also proves the real pool path.
