# Hybrid CPU+GPU demo - measurement report

Ground truth: 19 synthetic documents (seed 42, licences: synthetic, no personal data).
Device-time and dollar figures are SIMULATED where the fake pool backends were used; against real vLLM pools only request counts, routes and wall-clock hold.

## Arms overview

| arm | mode | docs | wall s | errors | fallback (degraded) |
|---|---|---|---|---|---|
| D | matrix | 20 | 3144.54 | 72 | 0 |

## Quality vs ground truth

| arm | extract P | extract R | extract F1 | risk acc | type acc | report acc | docs fully correct |
|---|---|---|---|---|---|---|---|
| D | 0.778 | 0.538 | 0.636 | 0.1 | None | 0.0 | 1/20 |
| *majority-class baseline* | — | — | — | *0.684* | — | — | — |

How to read the quality numbers:

- **Extraction** is a copy task: quote the clause. Small models do it as well as
  (here: better than) large ones, so the CPU pool takes the high-volume steps
  without a quality cost. Large models paraphrase, which exact-text scoring
  penalises even when the paraphrase is semantically right.
- **Risk accuracy** is rubric-following judgment. Compare it against the majority
  baseline (0.684 on this document set): a model at the baseline is not
  applying the rubric at all. A model below it is worse than always guessing
  'low'. This is where the CPU/GPU quality trade-off actually lives, and it is
  the number the agreed tolerance (V8) should be set against. Note the rubric is
  deterministic given the extractions - a rule in the pipeline would score ~1.0;
  the LLM step measures model judgment, not what the pipeline could hard-code.
- **Report accuracy** aggregates the per-document results, so it inherits every
  upstream error (propagation), plus its own synthesis errors.

## Classifier and sensitivity screen

| arm | labelled | complexity accuracy | screen tp/fp/tn/fn | screen precision | screen recall |
|---|---|---|---|---|---|
| D | 0 | None | 5/0/15/0 | 1.0 | 1.0 |

## Routing and governance

| arm | CPU-served share | sensitive steps | on approved pool | violations |
|---|---|---|---|---|
| D | 1.0 | 31 | 24 | 0 |

Violations count sensitive-document steps served by a pool outside the approved set (vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.

## Cost (device-time, then priced as a sweep - no single $ figure)

- device-time source: simulated (X-Busy-Ms headers from the fake pools)

| arm | GPU-s/doc | CPU core-s/doc |
|---|---|---|
| D | 0.0 | 0.0 |

No device-time headers captured (real vLLM backends); price from pool /metrics scrapes instead.

## Resilience

| arm | fallback (degraded) | errors |
|---|---|---|
| D | 0 | 72 |
