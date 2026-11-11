# Hybrid CPU+GPU demo - measurement report

Ground truth: 200 synthetic documents (seed 42, licences: synthetic, no personal data).
Each config's table row names the number of documents it actually ran.
Device-time is measured from the real pools' `/metrics` samples where the sampler succeeded; simulated `X-Busy-Ms` headers are labeled as such, and a run with neither prints `n/a`, never zero. Dollar figures are illustrative sweeps over the measured device time, not a quote.

## Configs overview

| config | mode | docs | wall s | errors | fallback (degraded) |
|---|---|---|---|---|---|
| direct/cpu-moe | direct | 200 | 2949.5 | 0 | 0 |
| direct/gpu-moe | direct | 200 | 301.6 | 0 | 0 |
| direct/cpu-small | direct | 200 | 1144.4 | 0 | 0 |

## Quality vs ground truth

| config | extract P (containment) | extract R (containment) | extract F1 | risk acc | type acc | report acc | docs fully correct |
|---|---|---|---|---|---|---|---|
| direct/cpu-moe | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0.746 | 200/200 |
| direct/gpu-moe | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0.76 | 200/200 |
| direct/cpu-small | 1.0 | 1.0 | 1.0 | 0.395 | 0.875 | 0.212 | 76/200 |
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
| direct/cpu-moe | non-sensitive | 159 | 1.0 | 1.0 | 1.0 | 159/159 | — |
| direct/cpu-moe | sensitive | 41 | 1.0 | 1.0 | 1.0 | 41/41 | — |
| direct/gpu-moe | non-sensitive | 159 | 1.0 | 1.0 | 1.0 | 159/159 | — |
| direct/gpu-moe | sensitive | 41 | 1.0 | 1.0 | 1.0 | 41/41 | — |
| direct/cpu-small | non-sensitive | 159 | 1.0 | 0.434 | 0.843 | 66/159 | — |
| direct/cpu-small | sensitive | 41 | 1.0 | 0.244 | 1.0 | 10/41 | — |

Class comes from the dataset's own `sensitive` label, never from the
pipeline's screen: the screen decides routing, so scoring against its own
decision would be circular. Enforcement (sensitive steps on non-approved
pools) is still counted from the screen's decisions in the next section.

## Classifier and sensitivity screen

| config | labelled | complexity accuracy | screen tp/fp/tn/fn | screen precision | screen recall |
|---|---|---|---|---|---|
| direct/cpu-moe | 0 | None | 0/0/0/0 | None | None |
| direct/gpu-moe | 0 | None | 0/0/0/0 | None | None |
| direct/cpu-small | 0 | None | 0/0/0/0 | None | None |

## Routing and governance

| config | CPU-served share | sensitive steps | on approved pool | violations |
|---|---|---|---|---|
| direct/cpu-moe | n/a | n/a | n/a | n/a |
| direct/gpu-moe | n/a | n/a | n/a | n/a |
| direct/cpu-small | n/a | n/a | n/a | n/a |

Violations count sensitive-document steps served by a pool outside the approved set (cuad-specialist, vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.

Direct run(s) direct/cpu-moe, direct/gpu-moe, direct/cpu-small carry no route headers: one bare endpoint receives every request, so routing and governance are not scored. `n/a` is not zero - by construction each direct run served all of its sensitive documents on that single pool.

## Cost (device-time, then priced as a sweep - no single $ figure)

- device-time source: unmeasured (direct/cpu-moe, direct/gpu-moe, direct/cpu-small)

| config | GPU-s/doc | CPU core-s/doc |
|---|---|---|
| direct/cpu-moe | n/a | n/a |
| direct/gpu-moe | n/a | n/a |
| direct/cpu-small | n/a | n/a |

- GPU-s/doc counts each physical backend once. Routes whose pool map declares `shared_with` (or a deployment marked `(shared with ...)`) are maxed, not summed.
- Route `vllm-gpu-small` is accounted as GPU but its pool map records CPU hardware; its GPU-s/doc is route-name accounting, not GPU device time.

Direct runs have no route headers, so their GPU/CPU split is not attributable either; `n/a` is not zero.

No config has both GPU and CPU device time measured; no dollar figure is quoted until the unmeasured pool is backfilled.

## Resilience

| config | fallback (degraded) | errors |
|---|---|---|
| direct/cpu-moe | 0 | 0 |
| direct/gpu-moe | 0 | 0 |
| direct/cpu-small | 0 | 0 |
