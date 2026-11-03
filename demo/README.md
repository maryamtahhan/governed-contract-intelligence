# hybrid-demo

Contract-review demo on hybrid CPU+GPU inference. See the repository root
[README](../README.md) for the architecture, the OpenShift and Linux runbooks,
and what is measured versus simulated.

Python package: synthetic contract generator, review pipeline, simulated vLLM
backends, the extended trace UI, and the measurement harness.

```bash
uv sync                     # create the venv (no torch: base install)
uv run pytest               # unit + local-stack tests
# The specialist pool and the CUAD/specialist validators need torch:
uv sync --extra specialist
```

Entry points (also used inside the containers and on OpenShift):

- `hybrid-gendata` - generate synthetic contracts + ground truth
- `hybrid-review` - run the contract-review batch against a gateway entry
- `hybrid-score` - build the measurement report; `governance` subcommand
  verifies sensitive-entry traces against the trace UI
- `hybrid-pool` - simulated vLLM replica (OpenAI-compatible subset) for stacks
  without real model backends
- `hybrid-traceui` - the extended trace UI / recording relay
