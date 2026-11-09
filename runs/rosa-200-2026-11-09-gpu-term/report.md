# Hybrid CPU+GPU demo - measurement report

Ground truth: 200 synthetic documents (seed 42, licences: synthetic, no personal data).
Each config's table row names the number of documents it actually ran.
Device-time is measured from the real pools' `/metrics` samples where the sampler succeeded; simulated `X-Busy-Ms` headers are labeled as such, and a run with neither prints `n/a`, never zero. Dollar figures are illustrative sweeps over the measured device time, not a quote.

## Configs overview

| config | mode | docs | wall s | errors | fallback (degraded) |
|---|---|---|---|---|---|
| hybrid | matrix | 200 | 726.77 | 0 | 0 |
| all-gpu | all-gpu | 200 | 202.83 | 0 | 0 |
| gpu-small | gpu-split | 200 | 454.21 | 0 | 0 |
| round-robin | round-robin | 200 | 570.81 | 0 | 0 |

## Quality vs ground truth

| config | extract P (containment) | extract R (containment) | extract F1 | risk acc | type acc | report acc | docs fully correct |
|---|---|---|---|---|---|---|---|
| hybrid | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0.756 | 200/200 |
| all-gpu | 1.0 | 1.0 | 1.0 | 0.995 | 1.0 | 0.756 | 199/200 |
| gpu-small | 1.0 | 1.0 | 1.0 | 0.395 | 0.865 | 0.278 | 75/200 |
| round-robin | 1.0 | 0.981 | 0.991 | 1.0 | 1.0 | 0.756 | 186/200 |
| *majority-class baseline* | — | — | — | *0.485* | — | — | — |

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
| hybrid | non-sensitive | 159 | 1.0 | 1.0 | 1.0 | 159/159 | vllm-cpu:636, vllm-gpu:318 |
| hybrid | sensitive | 41 | 1.0 | 1.0 | 1.0 | 41/41 | vllm-cpu:164, vllm-gpu-approved:82 |
| all-gpu | non-sensitive | 159 | 1.0 | 1.0 | 1.0 | 159/159 | vllm-gpu:954 |
| all-gpu | sensitive | 41 | 1.0 | 0.976 | 1.0 | 40/41 | vllm-gpu:246 |
| gpu-small | non-sensitive | 159 | 1.0 | 0.434 | 0.83 | 65/159 | vllm-gpu-small:954 |
| gpu-small | sensitive | 41 | 1.0 | 0.244 | 1.0 | 10/41 | vllm-gpu-small:246 |
| round-robin | non-sensitive | 159 | 0.99 | 1.0 | 1.0 | 147/159 | vllm-cpu:471, vllm-gpu:483 |
| round-robin | sensitive | 41 | 0.993 | 1.0 | 1.0 | 39/41 | vllm-cpu:129, vllm-gpu:117 |

Class comes from the dataset's own `sensitive` label, never from the
pipeline's screen: the screen decides routing, so scoring against its own
decision would be circular. Enforcement (sensitive steps on non-approved
pools) is still counted from the screen's decisions in the next section.

## Classifier and sensitivity screen

| config | labelled | complexity accuracy | screen tp/fp/tn/fn | screen precision | screen recall |
|---|---|---|---|---|---|
| hybrid | 0 | None | 41/0/159/0 | 1.0 | 1.0 |
| all-gpu | 0 | None | 41/0/159/0 | 1.0 | 1.0 |
| gpu-small | 0 | None | 41/0/159/0 | 1.0 | 1.0 |
| round-robin | 0 | None | 41/0/159/0 | 1.0 | 1.0 |

## Routing and governance

| config | CPU-served share | sensitive steps | on approved pool | violations |
|---|---|---|---|---|
| hybrid | 0.666 | 247 | 247 | 0 |
| all-gpu | 0.0 | 247 | 0 | 247 |
| gpu-small | 0.0 | 247 | 0 | 247 |
| round-robin | 0.5 | 247 | 129 | 118 |

Violations count sensitive-document steps served by a pool outside the approved set (vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.

Judgment split (risk + report through dedicated entries): config hybrid 201/201 on GPU.

Termination extraction routes (the CUAD specialist's question is Termination For Convenience, so termination for cause is sent to the general model): config hybrid vllm-gpu=159, vllm-gpu-approved=41; config all-gpu vllm-gpu=200; config gpu-small vllm-gpu-small=200; config round-robin vllm-cpu=100, vllm-gpu=100.

## Cost (device-time, then priced as a sweep - no single $ figure)

- device-time source: measured (hybrid, all-gpu, gpu-small, round-robin)

| config | GPU-s/doc | CPU core-s/doc |
|---|---|---|
| hybrid | 0.508 | 8.42 |
| all-gpu | 1.013 | — |
| gpu-small | 2.241 | — |
| round-robin | 1.773 | 6.65 |

- GPU-s/doc counts each physical backend once. Routes whose pool map declares `shared_with` (or a deployment marked `(shared with ...)`) are maxed, not summed.
- Route `vllm-gpu-small` is accounted as GPU but its pool map records CPU hardware; its GPU-s/doc is route-name accounting, not GPU device time.

Dollar sweep (ILLUSTRATIVE rates; replace with the agreed cost model before any number leaves this repo):

- config hybrid:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0005/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0007/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0004/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0002/document (gpu $1.0/h, core $0.04/h)
- config all-gpu:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0003/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0008/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0004/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0003/document (gpu $1.0/h, core $0.04/h)
- config gpu-small:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0007/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0019/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0009/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0006/document (gpu $1.0/h, core $0.04/h)
- config round-robin:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0008/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0017/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0008/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0006/document (gpu $1.0/h, core $0.04/h)

## Resilience

| config | fallback (degraded) | errors |
|---|---|---|
| hybrid | 0 | 0 |
| all-gpu | 0 | 0 |
| gpu-small | 0 | 0 |
| round-robin | 0 | 0 |
