"""Pool routing sets, shared by the pipeline, scorer and trace relay.

One definition so a pool rename cannot silently split the measurement from
the code that produced it.
"""
from __future__ import annotations

GPU_ROUTES = {"vllm-gpu", "vllm-gpu-approved", "vllm-gpu-small"}
CPU_ROUTES = {"vllm-cpu"}
APPROVED_ROUTES = {"vllm-cpu", "vllm-gpu-approved"}
