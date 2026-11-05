# Hybrid CPU+GPU demo - measurement report

Ground truth: 20 CUAD v1 real contracts (CUAD v1 (CC BY 4.0, verify before use); seed 42; licences: CC BY 4.0 - verify before redistribution).
Each arm's table row names the number of documents it actually ran.
Device-time and dollar figures are SIMULATED where the fake pool backends were used; against real vLLM pools only request counts, routes and wall-clock hold.

## Arms overview

| arm | mode | docs | wall s | errors | fallback (degraded) |
|---|---|---|---|---|---|
| D | matrix | 20 | 748.71 | 20 | 1 |
| D | matrix | 20 | 713.41 | 86 | 1 |
| D | matrix | 20 | 1099.57 | 82 | 0 |
| D | matrix | 20 | 1099.96 | 0 | 1 |

## Quality vs ground truth

| arm | extract P (containment) | extract R (containment) | extract F1 | risk acc | type acc | report acc | docs fully correct |
|---|---|---|---|---|---|---|---|
| D | 0.682 | 0.577 | 0.625 | 0.6 | None | 0.526 | 6/20 |
| D | 0.75 | 0.107 | 0.188 | 0.15 | None | 0.25 | 1/20 |
| D | 0.857 | 0.214 | 0.343 | 0.05 | None | 0.0 | 0/20 |
| D | 0.138 | 1.0 | 0.242 | 0.1 | None | 0.25 | 0/20 |
| *majority-class baseline* | — | — | — | *0.684* | — | — | — |

How to read the quality numbers:

- **Extraction** is a copy task: quote the clause. Small models do it as well as
  (here: better than) large ones, so the CPU pool takes the high-volume steps
  without a quality cost. Scoring is containment (normalised substring in
  either direction), not span-exact: a wider quote that contains the annotated
  clause passes, and a correct paraphrase fails.
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
| D | 10 | 0.1 | 0/0/0/0 | None | None |
| D | 23 | 0.565 | 0/0/0/0 | None | None |
| D | 9 | 0.0 | 0/0/0/0 | None | None |
| D | 23 | 0.696 | 5/0/15/0 | 1.0 | 1.0 |

**Classifier caveat:** arm D accuracy 0.1, arm D accuracy 0.565, arm D accuracy 0.0. At this accuracy the label does not separate the step types; a high CPU-served share reflects the CPU-first preference, not demonstrated complexity routing. Report the confusion matrix, not only the share.

Label confusion (truth->predicted, arm D): COMPLEX->MEDIUM=3, SIMPLE->MEDIUM=4, SIMPLE->SIMPLE=16

## Routing and governance

| arm | CPU-served share | sensitive steps | on approved pool | violations |
|---|---|---|---|---|
| D | 0.99 | 31 | 23 | 0 |
| D | 0.686 | 31 | 13 | 0 |
| D | 0.282 | 31 | 26 | 0 |
| D | 1.0 | 31 | 31 | 0 |

Violations count sensitive-document steps served by a pool outside the approved set (vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.

## Cost (device-time, then priced as a sweep - no single $ figure)

- device-time source: measured (exact (pool_busy_seconds_total counter))
- device-time source: measured (integrated (in-flight gauges; batching means the device serves several requests while occupied))

| arm | GPU-s/doc | CPU core-s/doc |
|---|---|---|
| D | 50.205 | 311.781 |
| D | 47.669 | 0.0 |
| D | 68.078 | 0.0 |
| D | 0.0 | 400.745 |

Dollar sweep (ILLUSTRATIVE rates; replace with the agreed cost model before any number leaves this repo):

- arm D:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0253/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0496/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0248/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0165/document (gpu $1.0/h, core $0.04/h)
- arm D:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0138/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0377/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0189/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0126/document (gpu $1.0/h, core $0.04/h)
- arm D:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0198/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0539/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0269/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.018/document (gpu $1.0/h, core $0.04/h)
- arm D:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0137/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0127/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0063/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0042/document (gpu $1.0/h, core $0.04/h)

## Resilience

| arm | fallback (degraded) | errors |
|---|---|---|
| D | 1 | 20 |
| D | 1 | 86 |
| D | 0 | 82 |
| D | 1 | 0 |
