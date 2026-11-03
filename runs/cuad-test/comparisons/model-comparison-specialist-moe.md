# Hybrid CPU+GPU demo - measurement report

Ground truth: 19 synthetic documents (seed 42, licences: synthetic, no personal data).
Device-time and dollar figures are SIMULATED where the fake pool backends were used; against real vLLM pools only request counts, routes and wall-clock hold.

## Arms overview

| arm | mode | docs | wall s | errors | fallback (degraded) |
|---|---|---|---|---|---|
| D | matrix | 20 | 2291.6 | 0 | 1 |
| D | matrix | 20 | 1591.47 | 11 | 0 |

## Quality vs ground truth

| arm | extract P | extract R | extract F1 | risk acc | type acc | report acc | docs fully correct |
|---|---|---|---|---|---|---|---|
| D | 0.636 | 0.808 | 0.712 | 0.65 | None | 0.105 | 6/20 |
| D | 0.633 | 0.76 | 0.691 | 0.45 | None | 0.126 | 3/20 |
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
| D | 104 | 0.481 | 5/0/15/0 | 1.0 | 1.0 |
| D | 5 | 0.2 | 5/0/15/0 | 1.0 | 1.0 |

Label confusion (truth->predicted, arm D): SIMPLE->COMPLEX=4, SIMPLE->SIMPLE=1

## Routing and governance

| arm | CPU-served share | sensitive steps | on approved pool | violations |
|---|---|---|---|---|
| D | 0.959 | 31 | 31 | 0 |
| D | 0.009 | 31 | 25 | 0 |

Violations count sensitive-document steps served by a pool outside the approved set (vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.

## Cost (device-time, then priced as a sweep - no single $ figure)

- device-time source: measured (exact (pool_busy_seconds_total counter))
- device-time source: measured (integrated (in-flight gauges; batching means the device serves several requests while occupied))

| arm | GPU-s/doc | CPU core-s/doc |
|---|---|---|
| D | 8.938 | 740.173 |
| D | 74.572 | 0.0 |

Dollar sweep (ILLUSTRATIVE rates; replace with the agreed cost model before any number leaves this repo):

- arm D:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.028/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0305/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0153/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0102/document (gpu $1.0/h, core $0.04/h)
- arm D:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0216/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.059/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0295/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0197/document (gpu $1.0/h, core $0.04/h)

## Resilience

| arm | fallback (degraded) | errors |
|---|---|---|
| D | 1 | 0 |
| D | 0 | 11 |
