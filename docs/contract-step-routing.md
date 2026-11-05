# Contract-step routing

**Status:** taxonomy and evaluation harness added; **not measured against the
contract workload and not deployed**. The existing complexity configuration and
the B judgment-split results remain the recorded evidence until a replacement
run is measured.

## Why a step taxonomy

The complexity taxonomy answers “how hard is this prompt?” That is the right
question for open-ended chat, where the gateway sees heterogeneous clients and
cannot know what the request is for. The contract pipeline is not that workload:
the orchestrator already knows which contract step it is running, and the four
steps have different quality, latency, and governance needs.

The replacement taxonomy labels the **task stage**, not the difficulty:

| Label | What the request is doing | Default placement |
| --- | --- | --- |
| `CLASSIFY` | Identify the contract type and initial term | CPU |
| `EXTRACT` | Locate and quote a named clause | CPU |
| `RISK` | Judge one contract's exposure | GPU / judgment entry |
| `REPORT` | Aggregate the batch into a portfolio report | GPU / judgment entry |

This is stage routing, not complexity routing. Do not describe it as complexity
routing and do not compare its numbers to the complexity arm's numbers without
saying which taxonomy produced them.

The taxonomy lives in [`classifiers/contract-step.json`](../classifiers/contract-step.json).
Its anchors describe task intent. They are deliberately paraphrased and none is
copied from the production prompt builders.

## Why the harness has several families

A classifier can score well on a taxonomy while learning nothing about the task.
The contract body is large and lexically stable across all four steps, so a
model that keys on “this is an MSA” can put every request in one class. The
harness makes that failure visible instead of averaging it away.

`demo/src/hybrid_demo/evalkit/contract_steps.py` builds 33 cases:

- **4 real prompts** from the actual pipeline builders: `classify_body`,
  `extract_body`, `risk_body`, and `report_body`.
- **16 paraphrases**, four held-out wordings per label. The task content stays
  fixed; the instruction changes. This is the generalisation test.
- **4 same-document controls**: one contract body under all four instructions.
  The expected label follows the instruction. A body-keyed classifier cannot
  pass this family.
- **4 long-document cases**: one ~15,000-character agreement under the same
  four instructions. The filter sees only the first 4096 characters, so the
  leading instruction must still carry the label. These are intentionally over
  budget; every other family must stay under it.
- **5 agent-shaped cases**: tool-result-only, assistant-only, and system-only
  requests have no user turn and must fall through without a label. Two more
  put the real task in the final user turn after a tool result or assistant
  turn; the label must follow that turn, not the payload.

The harness also checks for anchor leakage: an anchor that contains a whole test
prompt (or vice versa) would make the test circular.

## Running it

Validate the taxonomy and case set without a stack:

```bash
cd demo
uv run hybrid-eval-contract-steps --check
```

That prints the family counts, prompt-length warnings, and anchor-leakage list.
It must report `"anchor_leakage": []` and an empty
`"prompts_over_filter_budget": []`. The four `long_document_cases` are expected
to be over the 4096-character budget; no other case may be.

Run against a stack that has this taxonomy loaded and the `llm_d_sc` filter
emitting provenance headers:

```bash
cd demo
uv run hybrid-eval-contract-steps \
  --base-url http://127.0.0.1:8080 \
  --out runs/contract-step/eval.json
```

The runner sends the real prompt text through `/v1/chat/completions` and reads
`x-llm-d-sc-label` / `x-demo-classification` from the response. It does not read
the model's answer, so the upstream generation budget is capped to keep the run
cheap. Use `--fail-under 0.9` in CI once a threshold is agreed.

To replay the exact cases through another tool:

```bash
uv run hybrid-eval-contract-steps --check --cases-out runs/contract-step/cases.jsonl
```

Each JSONL record contains the body, the expected label, the family, and the
prompt.

## What counts as a pass

A usable result needs all of the following, not just a high overall accuracy:

1. `same_document_control` is 4/4. This is the non-negotiable control.
2. `long_document_control` is also 4/4. A taxonomy that only works on short
   prompts cannot route this pipeline.
3. `agent_no_user` is 3/3: a request with no user turn gets no invented label.
   The two agent cases with a final user turn are classified from that turn.
4. Every label has non-zero recall; a taxonomy that never predicts `REPORT` is
   not a four-way router.
5. The real-prompt and paraphrase families are both strong. A large gap means
   the classifier is matching template wording rather than the task.
6. No anchor leakage and no **unexpected** truncation. The long-document
   family is over budget by design and must still be classified from its
   truncated prefix; every other family must fit.
7. The score is from this taxonomy. A run against a complexity-backed llm-d-sc
   instance is not evidence for contract-step routing.

Report the confusion matrix and per-family counts with the accuracy. A single
accuracy number hides the exact failure the control exists to catch.

## Deploying it after it passes

The deployment is intentionally not switched yet. When the harness passes:

1. Mount `classifiers/contract-step.json` into the llm-d-sc pod (a ConfigMap
   volume is enough; this taxonomy is data, not new weights).
2. Set `LLM_D_SC_CLASSIFIER` to that mounted path instead of `complexity`.
3. Set the filter's `signal: contract_step` so it matches the taxonomy's
   `signal`. A mismatch is rejected by llm-d-sc as `INVALID_ARGUMENT`.
4. Replace the complexity label routes with `CLASSIFY`, `EXTRACT`, `RISK`, and
   `REPORT` routes, and keep a `default_cluster` for fail-open behaviour.
5. Keep the PII screen and the approved-pool rule unchanged. Sensitivity beats
   stage: a sensitive `RISK` or `REPORT` still uses an approved entry, and a
   sensitive document must never fall back to an unapproved pool because the
   stage route selected one.

The classifier instance serves one signal. If both complexity and contract-step
are needed, run two llm-d-sc instances, not one multi-signal instance.

## Praxis image

The current `quay.io/rmalone/praxis-ai-llm-d-sc:v0.7.0` image integrates the
filter into an older Praxis build. The rebuild path for Praxis 0.7.5 /
ai-gateway 0.9.0 is in
[`deploy/images/praxis-llm-d-sc/`](../deploy/images/praxis-llm-d-sc/README.md).
The image must be built and pushed before the base manifests are pointed at it;
until then the recorded stack still runs the older gateway.

## Claim boundary

Do not claim a measured contract-step result until the harness has been run
against this taxonomy and the same-document control passes. The earlier B
judgment-split result and the D all-CPU result were measured on the complexity
taxonomy (or without the classifier deciding the stage). They are not
contract-step results.
