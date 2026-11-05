# Hybrid CPU+GPU demo - measurement report

Ground truth: 200 synthetic documents (seed 42, licences: synthetic, no personal data).
Each config's table row names the number of documents it actually ran.
Device-time and dollar figures are SIMULATED where the fake pool backends were used; against real vLLM pools only request counts, routes and wall-clock hold.

## Configs overview

| config | mode | docs | wall s | errors | fallback (degraded) |
|---|---|---|---|---|---|
| all-gpu | all-gpu | 200 | 202.19 | 0 | 0 |
| gpu-small | gpu-split | 200 | 438.92 | 0 | 0 |
| round-robin | round-robin | 200 | 612.91 | 0 | 1 |
| hybrid | matrix | 200 | 1111.76 | 0 | 0 |

## Quality vs ground truth

| config | extract P (containment) | extract R (containment) | extract F1 | risk acc | type acc | report acc | docs fully correct |
|---|---|---|---|---|---|---|---|
| all-gpu | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0.756 | 200/200 |
| gpu-small | 1.0 | 1.0 | 1.0 | 0.395 | 0.89 | 0.596 | 77/200 |
| round-robin | 1.0 | 0.984 | 0.992 | 1.0 | 1.0 | 0.75 | 188/200 |
| hybrid | 1.0 | 0.964 | 0.982 | 0.995 | 1.0 | 0.764 | 173/200 |
| *majority-class baseline* | — | — | — | *0.485* | — | — | — |

How to read the quality numbers:

- **Extraction** is a copy task: quote the clause. Small models do it as well as
  (here: better than) large ones, so the CPU pool takes the high-volume steps
  without a quality cost. Scoring is containment (normalised substring in
  either direction), not span-exact: a wider quote that contains the annotated
  clause passes, and a correct paraphrase fails.
- **Risk accuracy** is rubric-following judgment. Compare it against the majority
  baseline (0.485 on this document set): a model at the baseline is not
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
| all-gpu | non-sensitive | 159 | 1.0 | 1.0 | 1.0 | 159/159 | vllm-gpu:954 |
| all-gpu | sensitive | 41 | 1.0 | 1.0 | 1.0 | 41/41 | vllm-gpu:246 |
| gpu-small | non-sensitive | 159 | 1.0 | 0.434 | 0.862 | 67/159 | vllm-gpu-small:954 |
| gpu-small | sensitive | 41 | 1.0 | 0.244 | 1.0 | 10/41 | vllm-gpu-small:246 |
| round-robin | non-sensitive | 159 | 0.992 | 1.0 | 1.0 | 149/159 | vllm-cpu:476, vllm-gpu:478 |
| round-robin | sensitive | 41 | 0.993 | 1.0 | 1.0 | 39/41 | vllm-cpu:124, vllm-gpu:122 |
| hybrid | non-sensitive | 159 | 0.979 | 0.994 | 1.0 | 135/159 | vllm-cpu:954 |
| hybrid | sensitive | 41 | 0.99 | 1.0 | 1.0 | 38/41 | vllm-cpu:246 |

Class comes from the dataset's own `sensitive` label, never from the
pipeline's screen: the screen decides routing, so scoring against its own
decision would be circular. Enforcement (sensitive steps on non-approved
pools) is still counted from the screen's decisions in the next section.

## Classifier and sensitivity screen

| config | labelled | complexity accuracy | screen tp/fp/tn/fn | screen precision | screen recall |
|---|---|---|---|---|---|
| all-gpu | 0 | None | 41/0/159/0 | 1.0 | 1.0 |
| gpu-small | 1201 | 0.001 | 41/0/159/0 | 1.0 | 1.0 |
| round-robin | 0 | None | 41/0/159/0 | 1.0 | 1.0 |
| hybrid | 1201 | 0.001 | 41/0/159/0 | 1.0 | 1.0 |

**Classifier caveat:** config gpu-small agreement 0.001, config hybrid agreement 0.001. This column compares the classifier's prompt-difficulty label with the dataset's per-step label: a low value means the two taxonomies differ, not that the classifier failed - read the confusion matrix. At this value a high CPU-served share reflects the CPU-first policy, not demonstrated four-way complexity separation.

Label confusion (truth->predicted, hybrid config): COMPLEX->MEDIUM=200, REASONING->REASONING=1, SIMPLE->MEDIUM=1000

## Routing and governance

| config | CPU-served share | sensitive steps | on approved pool | violations |
|---|---|---|---|---|
| all-gpu | 0.0 | 247 | 0 | 247 |
| gpu-small | 0.0 | 247 | 0 | 247 |
| round-robin | 0.5 | 247 | 125 | 122 |
| hybrid | 0.999 | 247 | 247 | 0 |

Violations count sensitive-document steps served by a pool outside the approved set (vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.

## Cost (device-time, then priced as a sweep - no single $ figure)

- device-time source: measured (all-gpu, gpu-small, round-robin, hybrid)

| config | GPU-s/doc | CPU core-s/doc |
|---|---|---|
| all-gpu | 1.974 | — |
| gpu-small | 2.218 | — |
| round-robin | 2.855 | n/a |
| hybrid | 0.036 | n/a |

Config(s) hybrid, round-robin: device time is **not measured** for a pool that served steps (the relay front-end does not expose vLLM's in-flight gauges), so no dollar figure is quoted; `n/a` is not zero.

Dollar sweep (ILLUSTRATIVE rates; replace with the agreed cost model before any number leaves this repo):

- config all-gpu:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0006/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0016/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0008/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0005/document (gpu $1.0/h, core $0.04/h)
- config gpu-small:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0007/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0018/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0009/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0006/document (gpu $1.0/h, core $0.04/h)

## Resilience

| config | fallback (degraded) | errors |
|---|---|---|
| all-gpu | 0 | 0 |
| gpu-small | 0 | 0 |
| round-robin | 1 | 0 |
| hybrid | 0 | 0 |
