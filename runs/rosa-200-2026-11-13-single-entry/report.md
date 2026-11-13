# Hybrid CPU+GPU demo - measurement report

Ground truth: 200 synthetic documents (seed 42, licences: synthetic, no personal data).
Each config's table row names the number of documents it actually ran.
Device-time is measured from the real pools' `/metrics` samples where the sampler succeeded; simulated `X-Busy-Ms` headers are labeled as such, and a run with neither prints `n/a`, never zero. Dollar figures are illustrative sweeps over the measured device time, not a quote.

## Configs overview

| config | mode | docs | wall s | errors | fallback (degraded) |
|---|---|---|---|---|---|
| single-entry | matrix | 200 | 809.88 | 0 | 0 |

## Quality vs ground truth

| config | extract P (containment) | extract R (containment) | extract F1 | risk acc | type acc | report acc | docs fully correct |
|---|---|---|---|---|---|---|---|
| single-entry | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 200/200 |
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
| single-entry | non-sensitive | 159 | 1.0 | 1.0 | 1.0 | 159/159 | cuad-specialist:477, vllm-cpu:318, vllm-gpu:159 |
| single-entry | sensitive | 41 | 1.0 | 1.0 | 1.0 | 41/41 | cuad-specialist:123, vllm-cpu:82, vllm-gpu-approved:41 |

Class comes from the dataset's own `sensitive` label, never from the
pipeline's screen: the screen decides routing, so scoring against its own
decision would be circular. Enforcement (sensitive steps on non-approved
pools) is still counted from the screen's decisions in the next section.

## Classifier and sensitivity screen

| config | labelled | complexity accuracy | screen tp/fp/tn/fn | screen precision | screen recall |
|---|---|---|---|---|---|
| single-entry | 0 | None | 41/0/159/0 | 1.0 | 1.0 |

## Routing and governance

| config | CPU-served share | sensitive steps | on approved pool | violations |
|---|---|---|---|---|
| single-entry | 0.833 | 247 | 246 | 1 |

Violations count sensitive-document steps served by a pool outside the approved set (cuad-specialist, vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.

Judgment routing (risk + report through GPU routes): config single-entry 201/201 on GPU.

Termination extraction routes (CPU specialist; the canonical CUAD convenience question is asked first and a cause follow-up only when it finds nothing and the contract has cause language): config single-entry cuad-specialist=200.

## Cost (device-time, then priced as a sweep - no single $ figure)

- device-time source: measured (single-entry)

| config | GPU-s/doc | CPU core-s/doc |
|---|---|---|
| single-entry | 0.258 | 32.533 |

- GPU-s/doc counts each physical backend once. Routes whose pool map declares `shared_with` (or a deployment marked `(shared with ...)`) are maxed, not summed.
- Route `vllm-gpu-small` is accounted as GPU but its pool map records CPU hardware; its GPU-s/doc is route-name accounting, not GPU device time.

Dollar sweep (ILLUSTRATIVE rates; replace with the agreed cost model before any number leaves this repo):

- config single-entry:
  - cloud list (gpu $1.1/h, core $0.13/h): $0.0013/document (gpu $1.1/h, core $0.13/h)
  - owned hardware @ 25% utilisation: $0.0013/document (gpu $3.0/h, core $0.12/h)
  - owned hardware @ 50% utilisation: $0.0006/document (gpu $1.5/h, core $0.06/h)
  - owned hardware @ 75% utilisation: $0.0004/document (gpu $1.0/h, core $0.04/h)

## Resilience

| config | fallback (degraded) | errors |
|---|---|---|
| single-entry | 0 | 0 |
