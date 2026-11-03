# Hybrid CPU+GPU demo - measurement report

Ground truth: 200 synthetic documents (seed 42, licences: synthetic, no personal data).
Device-time and dollar figures are SIMULATED where the fake pool backends were used; against real vLLM pools only request counts, routes and wall-clock hold.

## Arms overview

| arm | mode | docs | wall s | errors | fallback (degraded) |
|---|---|---|---|---|---|
| A | all-gpu | 20 | 190.77 | 0 | 0 |
| B | gpu-split | 20 | 58.63 | 0 | 0 |
| C | round-robin | 20 | 345.37 | 0 | 0 |
| D | matrix | 20 | 494.09 | 0 | 0 |

## Quality vs ground truth

| arm | extract P | extract R | extract F1 | risk acc | type acc | report acc | docs fully correct |
|---|---|---|---|---|---|---|---|
| A | 1.0 | 0.932 | 0.965 | 0.7 | 1.0 | 0.6 | 13/20 |
| B | 1.0 | 1.0 | 1.0 | 0.4 | 0.9 | 0.34 | 8/20 |
| C | 1.0 | 1.0 | 1.0 | 0.4 | 1.0 | 0.34 | 8/20 |
| D | 1.0 | 1.0 | 1.0 | 0.4 | 0.85 | 0.34 | 8/20 |
| *majority baseline (always low)* | — | — | — | *0.5* | — | — | — |

How to read the quality numbers:

- **Extraction** is a copy task: quote the clause. Small models do it as well as
  (here: better than) large ones, so the CPU pool takes the high-volume steps
  without a quality cost. Large models paraphrase, which exact-text scoring
  penalises even when the paraphrase is semantically right.
- **Risk accuracy** is rubric-following judgment. Compare it against the majority
  baseline (0.5 on this document set): a model at the baseline is not
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
| A | 0 | None | 4/0/16/0 | 1.0 | 1.0 |
| B | 121 | 0.008 | 4/0/16/0 | 1.0 | 1.0 |
| C | 1 | 0.0 | 4/0/16/0 | 1.0 | 1.0 |
| D | 121 | 0.008 | 4/0/16/0 | 1.0 | 1.0 |

Label confusion (truth->predicted, arm D): COMPLEX->MEDIUM=20, REASONING->REASONING=1, SIMPLE->MEDIUM=100

## Routing and governance

| arm | CPU-served share | sensitive steps | on approved pool | violations |
|---|---|---|---|---|
| A | 0.0 | 25 | 0 | 25 |
| B | 0.0 | 25 | 0 | 25 |
| C | 0.496 | 25 | 12 | 13 |
| D | 0.992 | 25 | 25 | 0 |

Violations count sensitive-document steps served by a pool outside the approved set (vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.

## Cost (device-time, then priced as a sweep - no single $ figure)

- device-time source: measured (integrated (in-flight gauges; batching means the device serves several requests while occupied))

| arm | GPU-s/doc | CPU core-s/doc |
|---|---|---|
| A | 9.354 | 0.0 |
| B | 2.983 | 0.0 |
| C | 7.058 | 121.732 |
| D | 0.208 | 194.728 |

Dollar sweep (ILLUSTRATIVE rates; replace with the agreed cost model before any number leaves this repo):

- arm A:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0029/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0078/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0039/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0026/document (gpu $1.0/h, core $0.04/h)
- arm B:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0009/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0025/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0012/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0008/document (gpu $1.0/h, core $0.04/h)
- arm C:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0066/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0099/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.005/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0033/document (gpu $1.0/h, core $0.04/h)
- arm D:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0071/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0067/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0033/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0022/document (gpu $1.0/h, core $0.04/h)

## Resilience

| arm | fallback (degraded) | errors |
|---|---|---|
| A | 0 | 0 |
| B | 0 | 0 |
| C | 0 | 0 |
| D | 0 | 0 |
