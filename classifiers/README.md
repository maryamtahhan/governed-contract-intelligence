# Classifier definitions

This directory holds llm-d-sc taxonomy definitions that are not compiled into
the upstream llm-d-sc binary. Note that **llm-d-sc is not part of the base
stack** (removed 2026-11-05; the Deployment is preserved in git history at
commit `4ac0551`) — these definitions are data for the experiment that would
re-add it.

- [`contract-step.json`](contract-step.json) labels the four contract pipeline
  stages (`CLASSIFY`, `EXTRACT`, `RISK`, `REPORT`). It is a **candidate**
  taxonomy: evaluate it before routing on it. See
  [`docs/design.md`](../docs/design.md) §5.

The definition is mounted as a file and selected with
`LLM_D_SC_CLASSIFIER=/path/to/contract-step.json`. The `model_repo` and
`model_revision` fields are metadata only; llm-d-sc loads the actual embedding
weights from `LLM_D_SC_MODEL_DIR`.

The `signal` field is `contract_step`. If PRAXIS sends a different signal, or a
filter config sets `signal:` to another value, llm-d-sc returns
`INVALID_ARGUMENT`; set `signal: contract_step` in the filter only when this
taxonomy is the one loaded.
