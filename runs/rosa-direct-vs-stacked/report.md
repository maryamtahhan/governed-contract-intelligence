# Hybrid CPU+GPU demo - measurement report

Ground truth: 200 synthetic documents (seed 42, licences: synthetic, no personal data).
Each arm's table row names the number of documents it actually ran.
Device-time and dollar figures are SIMULATED where the fake pool backends were used; against real vLLM pools only request counts, routes and wall-clock hold.

## Arms overview

| arm | mode | docs | wall s | errors | fallback (degraded) |
|---|---|---|---|---|---|
| cpu-moe | direct | 200 | 2095.2 | 0 | 0 |
| gpu-moe | direct | 200 | 228.3 | 0 | 0 |
| cpu-small | direct | 200 | 594.3 | 0 | 0 |
| D | matrix | 200 | 1111.76 | 0 | 0 |

## Quality vs ground truth

| arm | extract P (containment) | extract R (containment) | extract F1 | risk acc | type acc | report acc | docs fully correct |
|---|---|---|---|---|---|---|---|
| cpu-moe | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0.746 | 200/200 |
| gpu-moe | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0.768 | 200/200 |
| cpu-small | 1.0 | 1.0 | 1.0 | 0.395 | 0.895 | 0.212 | 77/200 |
| D | 1.0 | 0.964 | 0.982 | 0.995 | 1.0 | 0.764 | 173/200 |
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

| arm | class | docs | extract F1 | risk acc | type acc | docs fully correct | routes |
|---|---|---|---|---|---|---|---|
| cpu-moe | non-sensitive | 159 | 1.0 | 1.0 | 1.0 | 159/159 | — |
| cpu-moe | sensitive | 41 | 1.0 | 1.0 | 1.0 | 41/41 | — |
| gpu-moe | non-sensitive | 159 | 1.0 | 1.0 | 1.0 | 159/159 | — |
| gpu-moe | sensitive | 41 | 1.0 | 1.0 | 1.0 | 41/41 | — |
| cpu-small | non-sensitive | 159 | 1.0 | 0.434 | 0.868 | 67/159 | — |
| cpu-small | sensitive | 41 | 1.0 | 0.244 | 1.0 | 10/41 | — |
| D | non-sensitive | 159 | 0.979 | 0.994 | 1.0 | 135/159 | vllm-cpu:954 |
| D | sensitive | 41 | 0.99 | 1.0 | 1.0 | 38/41 | vllm-cpu:246 |

Class comes from the dataset's own `sensitive` label, never from the
pipeline's screen: the screen decides routing, so scoring against its own
decision would be circular. Enforcement (sensitive steps on non-approved
pools) is still counted from the screen's decisions in the next section.

## Classifier and sensitivity screen

| arm | labelled | complexity accuracy | screen tp/fp/tn/fn | screen precision | screen recall |
|---|---|---|---|---|---|
| cpu-moe | 0 | None | 0/0/0/0 | None | None |
| gpu-moe | 0 | None | 0/0/0/0 | None | None |
| cpu-small | 0 | None | 0/0/0/0 | None | None |
| D | 1201 | 0.001 | 41/0/159/0 | 1.0 | 1.0 |

**Classifier caveat:** arm D agreement 0.001. This column compares the classifier's prompt-difficulty label with the dataset's per-step label: a low value means the two taxonomies differ, not that the classifier failed - read the confusion matrix. At this value a high CPU-served share reflects the routing preference, not demonstrated four-way complexity separation.

Label confusion (truth->predicted, arm D): COMPLEX->MEDIUM=200, REASONING->REASONING=1, SIMPLE->MEDIUM=1000

## Routing and governance

| arm | CPU-served share | sensitive steps | on approved pool | violations |
|---|---|---|---|---|
| cpu-moe | n/a | n/a | n/a | n/a |
| gpu-moe | n/a | n/a | n/a | n/a |
| cpu-small | n/a | n/a | n/a | n/a |
| D | 0.999 | 247 | 247 | 0 |

Violations count sensitive-document steps served by a pool outside the approved set (vllm-cpu, vllm-gpu-approved). Target for the governed arm: zero.

Direct arm(s) cpu-moe, gpu-moe, cpu-small carry no route headers: one bare endpoint receives every request, so routing and governance are not scored. `n/a` is not zero - by construction each direct arm served all of its sensitive documents on that single pool.

## Cost (device-time, then priced as a sweep - no single $ figure)

- device-time source: measured (D)
- device-time source: unmeasured (cpu-moe, gpu-moe, cpu-small)

| arm | GPU-s/doc | CPU core-s/doc |
|---|---|---|
| cpu-moe | n/a | n/a |
| gpu-moe | n/a | n/a |
| cpu-small | n/a | n/a |
| D | 0.036 | n/a |

Arm(s) D: device time is **not measured** for a pool that served steps (the relay front-end does not expose vLLM's in-flight gauges), so no dollar figure is quoted; `n/a` is not zero.

Direct arms have no route headers, so their GPU/CPU split is not attributable either; `n/a` is not zero.

No arm has both GPU and CPU device time measured; no dollar figure is quoted until the unmeasured pool is backfilled.

## Resilience

| arm | fallback (degraded) | errors |
|---|---|---|
| cpu-moe | 0 | 0 |
| gpu-moe | 0 | 0 |
| cpu-small | 0 | 0 |
| D | 0 | 0 |
