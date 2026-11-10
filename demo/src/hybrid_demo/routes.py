"""Route classification sets for scoring and trace labels — not dispatch.

The pipeline never picks a pool: Praxis maps ``X-Demo-Step`` to a route in
``manifests/base/praxis/config.yaml``. These sets only classify the route a
trace already reports, so a pool rename cannot silently split the measurement
from the code that produced it.
"""
from __future__ import annotations

GPU_ROUTES = {"vllm-gpu", "vllm-gpu-approved", "vllm-gpu-small"}
CPU_ROUTES = {"vllm-cpu", "cuad-specialist"}
APPROVED_ROUTES = {"vllm-cpu", "cuad-specialist", "vllm-gpu-approved"}
