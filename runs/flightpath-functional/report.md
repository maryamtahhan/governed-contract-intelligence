# Hybrid CPU+GPU demo - measurement report

Ground truth: 200 synthetic documents (seed 42, licences: synthetic, no personal data).
Each config's table row names the number of documents it actually ran.
Device-time and dollar figures are SIMULATED where the fake pool backends were used; against real vLLM pools only request counts, routes and wall-clock hold.

## Configs overview

| config | mode | docs | wall s | errors | fallback (degraded) |
|---|---|---|---|---|---|
| all-gpu | all-gpu | 12 | 19.91 | 0 | 0 |
| gpu-small | gpu-split | 12 | 15.03 | 0 | 0 |
| round-robin | round-robin | 12 | 26.29 | 0 | 0 |
| hybrid | matrix | 12 | 28.16 | 0 | 0 |

## Quality vs ground truth

| config | extract P (containment) | extract R (containment) | extract F1 | risk acc | type acc | report acc | docs fully correct |
|---|---|---|---|---|---|---|---|
| all-gpu | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0.567 | 12/12 |
| gpu-small | 0.978 | 1.0 | 0.989 | 0.667 | 0.917 | 0.667 | 6/12 |
| round-robin | 0.977 | 0.977 | 0.977 | 1.0 | 0.917 | 0.567 | 10/12 |
| hybrid | 0.929 | 0.929 | 0.929 | 0.833 | 0.917 | 0.567 | 7/12 |
| *majority-class baseline* | — | — | — | *0.5* | — | — | — |

How to read the quality numbers:

- **Extraction** is a copy task: quote the clause. Small models do it as well as
  (here: better than) large ones, so the CPU pool takes the high-volume steps
  without a quality cost. Scoring is containment (normalised substring in
  either direction), not span-exact: a wider quote that contains the annotated
  clause passes, and a correct paraphrase fails.
  The JSON alongside this report also carries `exact` (string equality) and
  `partial` (token Jaccard) views, plus nervaluate's strict/exact/partial/type
  offset strategies when document text and nervaluate are available. The table
  keeps containment as the primary number so historical runs stay comparable.
- **Risk accuracy** is rubric-following judgment. Compare it against the majority
  baseline (0.5 on this document set): a model at the baseline is not
  applying the rubric at all. A model below it is worse than always guessing
  'low'. This is where the CPU/GPU quality trade-off actually lives, and it is
  the number the agreed tolerance (V8) should be set against. Note the rubric is
  deterministic given the extractions - a rule in the pipeline would score ~1.0;
  the LLM step measures model judgment, not what the pipeline could hard-code.
- **Report accuracy** aggregates the per-document results, so it inherits every
  upstream error (propagation), plus its own synthesis errors.

## Sensitive vs non-sensitive documents

| config | class | docs | extract F1 | risk acc | type acc | docs fully correct | routes |
|---|---|---|---|---|---|---|---|
| all-gpu | non-sensitive | 10 | 1.0 | 1.0 | 1.0 | 10/10 | vllm-gpu:60 |
| all-gpu | sensitive | 2 | 1.0 | 1.0 | 1.0 | 2/2 | vllm-gpu:12 |
| gpu-small | non-sensitive | 10 | 0.987 | 0.7 | 0.9 | 5/10 | vllm-gpu-small:60 |
| gpu-small | sensitive | 2 | 1.0 | 0.5 | 1.0 | 1/2 | vllm-gpu-small:12 |
| round-robin | non-sensitive | 10 | 0.973 | 1.0 | 0.9 | 8/10 | vllm-cpu:31, vllm-gpu:29 |
| round-robin | sensitive | 2 | 1.0 | 1.0 | 1.0 | 2/2 | vllm-cpu:5, vllm-gpu:7 |
| hybrid | non-sensitive | 10 | 0.914 | 0.8 | 0.9 | 5/10 | vllm-cpu:50, vllm-gpu:10 |
| hybrid | sensitive | 2 | 1.0 | 1.0 | 1.0 | 2/2 | vllm-cpu:10, vllm-gpu-approved:2 |

Class comes from the dataset's own `sensitive` label, never from the
pipeline's screen: the screen decides routing, so scoring against its own
decision would be circular. Enforcement (sensitive steps on non-approved
pools) is still counted from the screen's decisions in the next section.

## Classifier and sensitivity screen

| config | labelled | complexity accuracy | screen tp/fp/tn/fn | screen precision | screen recall |
|---|---|---|---|---|---|
| all-gpu | 0 | None | 2/0/10/0 | 1.0 | 1.0 |
| gpu-small | 0 | None | 2/0/10/0 | 1.0 | 1.0 |
| round-robin | 0 | None | 2/0/10/0 | 1.0 | 1.0 |
| hybrid | 0 | None | 2/0/10/0 | 1.0 | 1.0 |

## Routing and governance

| config | CPU-served share | sensitive steps | on approved pool | violations |
|---|---|---|---|---|
| all-gpu | 0.0 | 13 | 0 | 13 |
| gpu-small | 0.0 | 13 | 0 | 13 |
| round-robin | 0.493 | 13 | 5 | 8 |
| hybrid | 0.822 | 13 | 13 | 0 |

Violations count sensitive-document steps served by a pool outside the approved set (vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.

Judgment split (risk + report through dedicated entries): config hybrid 13/13 on GPU.

## Cost (device-time, then priced as a sweep - no single $ figure)

- device-time source: unmeasured (all-gpu, gpu-small, round-robin, hybrid)

| config | GPU-s/doc | CPU core-s/doc |
|---|---|---|
| all-gpu | 2.6 | — |
| gpu-small | 1.091 | — |
| round-robin | 1.287 | 23.372 |
| hybrid | 0.301 | 36.589 |

Dollar sweep (ILLUSTRATIVE rates; replace with the agreed cost model before any number leaves this repo):

- config all-gpu:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0008/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0022/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0011/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0007/document (gpu $1.0/h, core $0.04/h)
- config gpu-small:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0003/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0009/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0005/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0003/document (gpu $1.0/h, core $0.04/h)
- config round-robin:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0012/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0018/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0009/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0006/document (gpu $1.0/h, core $0.04/h)
- config hybrid:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0014/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0015/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0007/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0005/document (gpu $1.0/h, core $0.04/h)

## Resilience

| config | fallback (degraded) | errors |
|---|---|---|
| all-gpu | 0 | 0 |
| gpu-small | 0 | 0 |
| round-robin | 0 | 0 |
| hybrid | 0 | 0 |
