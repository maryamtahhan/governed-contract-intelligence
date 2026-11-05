# OCTO Demo: Private, Governed Agents on Hybrid CPU + GPU Inference

Demo plan and business case · **Draft v0.3 · measured on the ROSA demo stack**
(2026-10-29 → 2026-11-04)

Every number in this document comes from our own measurements; evidence packs
and protocols live in the source repository (`FINDINGS.md` + `runs*/`). Claims
are labelled **[measured]** (our numbers, our replay), **[external]** (cited,
not ours), or **[in flight]**. v0.1's rule — no performance or cost figures
until measured — is now satisfied; the figures below are the point of v0.2.

**What changed since v0.1, in one paragraph:** the validation phase ran, on
three environments (Linux/podman, a g5.12xlarge with real GPUs, and OpenShift
SNO), against expert-annotated real contracts (CUAD), with a four-arm
measurement design; the demo itself is being re-baselined on a managed ROSA
HCP stack (§3). Every value hypothesis in §1 held. The architecture
settled on the Praxis gateway with the llm-d-sc semantic classifier (V13's
"unknown gateway" resolved). The demo scenario has since been extended from
single-contract review toward **contract portfolio intelligence**
(retrieval-first, evidence-first) after retrieval measurements showed method
beating backbone — that pivot and its first end-to-end number are in §2b.
What is still open is listed honestly in §6.

**v0.2.1 (2026-11-04) — evidence-base corrections.** This revision adds no
claims; it removes numbers the 2026-11-03 measurement review superseded
(FINDINGS §13/§14) and names the environment the demo will actually run on.
The CUAD held-out set is **19 unique contracts** (the manifest's 20th entry
is a `doc_id` collision); the corrected 35B-A3B test-split score is
**F1 0.690** (the earlier 0.712 came from a different field set and is not
comparable); the 0.761 "mixed split" figure is no longer quoted because that
split contains training documents; and the 0.800 specialist result is a
**local direct run, not a through-stack measurement**. The demo target is
now a ROSA HCP cluster with an x8i.24xlarge CPU node and a g5.12xlarge GPU
node colocated in one AZ. The SNO numbers remain the validation record; the
ROSA re-baseline is in flight (§6).

**v0.3 (2026-11-04) — the demo stack, measured.** Every demo number now
comes from ROSA HCP `<redacted-cluster>`: the 200-document matrix (four routing configs),
the sensitivity split, the per-pool direct baseline, and the specialist
through the deployed stack. The headline is **containment + GPU-first
economics**: arm D keeps 247/247 sensitive steps on approved pools with 0
violations, serves 99.9% of steps on CPU, and spends 0.036 GPU-s/doc — 54×
less GPU time than all-GPU — for 1.8 F1 and 0.5 risk points. The classifier's
0.1% "accuracy" is a taxonomy mismatch, not a degraded model (§6.2): it
separates single-document analysis (CPU) from cross-document synthesis (GPU),
which is the boundary the policy needs. The direct baseline (§6.3) holds the
same weights on CPU and GPU to the same quality, so the governed path's cost
is speed, not accuracy.

**Naming.** The CLI and the new run directories use policy names. Where older
text in this document says “arm D”, read **hybrid config**; arm A is
**all-gpu**, arm B is **gpu-small**, and arm C is **round-robin**. Historical
run ids keep their recorded names.

## Results in plain language

For readers who don't work in this stack day to day: what we set out to
prove, and what we found — without the jargon. Every statement below is
backed by the technical sections (§5–§6) and recorded evidence; this part
is the translation, not a softer set of claims.

**What we built.** A working system that reviews contracts the way a
legal-operations team would: it checks each document for personal data,
pulls out the clauses that matter (termination rights, liability caps,
governing law), flags risk, and writes a summary across the batch. Every
request passes a gateway — think of a traffic warden — that decides, per
step, whether it runs on ordinary server hardware (CPU) or a GPU, and
whether the document is sensitive enough to be locked to pre-approved
in-house systems.

**"Can this run on the servers we already own, instead of scarce GPUs?"**
Yes, and you don't lose quality. In the governed configuration, ~99% of the
work ran on CPU, and on the hardest quality test — comparing the system's
quoted clauses against expert human annotations — the CPU configuration
scored identically to the GPU one (F1 0.690 each, same weights). The
trade-off is speed: minutes per document on CPU
rather than seconds. For batch contract review that's acceptable — nobody
is waiting on the result in real time.

**"Is it cheaper?"**
We measured the compute actually used. On the ROSA demo stack the hybrid
arm consumed **54× less GPU time per document** than all-GPU — 0.036 vs
1.974 GPU-seconds per document over 200 documents (7.3 vs 394.8 GPU-s in
total); the earlier g5 validation measured 45× (0.21 vs 9.35). At the
illustrative $1.10/GPU-hour that is roughly **$0.002 of GPU time for 200
contracts versus $0.12** all-GPU. On hardware you already own, the saving
is the GPU capacity you didn't have to buy or queue for.

**"What happens when the GPUs are down?"**
The service keeps going. Routine work continues on CPU, unaffected.
Harder questions either wait, or get answered by the CPU model with a
visible "degraded" marker — so nobody mistakes a fallback for the full
service. And documents containing personal data are never quietly sent
somewhere unsafe: if the approved system is unavailable, the request
**fails loudly rather than spilling**. We tested this by shutting the GPU
pool off mid-run and watching the logs.

**"Can you prove private documents stayed private?"**
This is the strongest result in the demo: on every stack and every document
set tested, **zero** requests that the pipeline's screen labelled sensitive
were handled outside the approved systems. And we didn't take the software's
word for it: the proof comes from the gateway's own audit records, like
checking bank statements rather than asking the bank. Two limits stated
plainly: the label is the screen's, not a third-party PII annotation (CUAD
ships none), so this measures containment and routing, not screen accuracy;
and the count covers recorded traces, which the relays guarantee for pool
traffic by construction.

**"Is the quality actually good?"**
Three answers, plus context:
- The small cheap model is *dangerously confident*: it always answers and
  is usually wrong — quoting the wrong paragraph with no sign of doubt.
  That is the worst possible failure shape for legal work.
- The large general model is decent: it found the right clause about
  7 times in 10.
- The purpose-built approach — first *retrieve* the relevant passages,
  then have a specialist quote them exactly — was the best: **8 times in
  10, and it cannot invent quotes**, because it can only copy text that is
  actually in the contract. That is the approach the product now builds on.
- For context: the best published systems on this benchmark score roughly
  8.8 in 10, and part of anyone's gap is noise in the human annotations
  themselves.

**"Did you just get lucky with one test set?"**
We guarded against that. Every quoted number comes from a held-out test
set the models had never seen, scored against expert annotations, with
every configuration run on exactly the same documents. Where differences
are small we ran statistical tests: the headline comparisons are
significant, not luck. One honest limitation — several measurements are
single runs, labelled as such in the technical record.

**"What is the sandboxed agent for?"**
The demo's agent — the thing that would actually assist a legal-ops team —
runs inside a locked container: its network is off except for one door to
the governed gateway. Every call it makes is checked and recorded; anything
else it tries — sending data somewhere, reaching the internet — is refused
and logged. The same batch review can therefore be triggered by a plain
script or by the agent, and the gateway treats them identically. The
sandbox answers the question the gateway cannot: *what if the agent itself
misbehaves?* (Wiring the agent as the live trigger of the batch is the open
"tool-calling" item.)

**"What's still not proven?"**
- The agent using external *tools* end-to-end (bookmarked, not tested).
- The full 200-document run "at scale" and speed-under-load tests.
- The "thinking mode" quality upgrade for the judgment model.
- The Gaudi accelerators on the OpenShift cluster are dead — a driver
  problem under investigation with Intel. Irrelevant to our results, which
  used CPUs and standard GPUs.
- A few final runs were still completing when this document was written;
  the technical record (§6) lists them.

## 1. Business case

*(Unchanged from v0.1 — the problem statement survived contact with
measurement. The value hypotheses below now carry results.)*

Regulated organisations (financial services, healthcare, public sector, legal)
want agentic AI on their most sensitive documents, but: data can't leave; GPUs
are scarce and expensive; agents are risky; single pools are single points of
failure. The proposal — a self-managed inference platform running agentic
pipelines on a hybrid pool, with a label-routing gateway and a sandboxed agent
runtime — was built and measured.

**Value hypotheses — v0.1 said "to be tested, not claimed". Results:**

| # | Hypothesis (v0.1) | Result |
|---|---|---|
| 1 | A meaningful share of agent steps can run on CPU with acceptable quality | **Confirmed [measured].** ~99% of steps CPU-served in the governed arm; the 35B-A3B MoE on CPU matched the same weights on 4× A10G on the held-out split (F1 0.690, risk 0.632, 5/19 fully correct on both), and the 1.5B scores 0.242. The retrieval-first specialist reaches 0.800 (local direct run, not through-stack; §2b) |
| 2 | This reduces GPU-seconds per completed task vs all-GPU | **Confirmed [measured].** ROSA, 200 synthetic documents: 0.036 vs 1.974 GPU-s/document (54×; 7.3 vs 394.8 GPU-s total) at equal-or-near quality. Earlier g5 validation: 0.21 vs 9.35 (45×). A 200-document batch is roughly $0.002 of GPU time hybrid vs $0.12 all-GPU at the illustrative $1.10/GPU-hour |
| 3 | Graceful degradation when the GPU pool is saturated or removed | **Confirmed [measured].** SIMPLE traffic unaffected; REASONING degrades to CPU with a visible flag; SENSITIVE+REASONING fails closed (explicit 502, never spilled) — verified from the gateway's own trace records |
| 4 (new) | Sensitive documents never leave the approved pool set | **Confirmed [measured] — the headline result.** Zero sensitive violations in the governed arm, on every stack, every dataset; the naive arms score 12–25 |

One strategic finding changed the pitch's shape: **contract review is
batch** — minutes per document on CPU tiers is operationally acceptable, so
the original "latency justifies GPU" argument is retired and is not made on
stage. GPU's remaining claims are interactive drill-down, burst absorption,
and unproven quality headroom (thinking mode).

## 2. Demo scenario

"Contract review at scale": a legal-operations team drops a batch of
contracts; the pipeline screens for personal data, extracts key clauses,
flags risks, and produces a cross-document summary. Each step is classified
and routed per step, not hand-wired per agent. **Sensitivity is a constraint;
complexity is a preference** — the two-entry routing matrix was implemented
exactly as proposed and is the demo's core governance artefact.

Quality against expert ground truth (CUAD, held-out test split, **19 unique
contracts** — the manifest's 20th entry is a `doc_id` collision — same fields
and scorer for every number) **[measured]**:

| configuration | extract P | extract R | extract F1 |
|---|---|---|---|
| 1.5B generative, CPU pool | 0.156 | 1.000 | 0.242 |
| 35B-A3B MoE generative, CPU pool | 0.625 | 0.769 | 0.690 |
| 35B-A3B MoE, same weights on 4× A10G | 0.625 | 0.769 | 0.690 |
| CUAD specialist (RoBERTa-large), full document — *local direct run* | 0.657 | 0.958 | 0.780 |
| **specialist on top-5 retrieved chunks (litil-embed-0.6b) — *local direct run*** | 0.667 | 1.000 | **0.800** |

The last row is the pivot's first end-to-end number: retrieve-then-extract
with a fine-tuned specialist that **cannot hallucinate by construction** (it
returns spans of the source document) beats the generative MoE by 11 F1
points (0.800 vs 0.690) at milliseconds per clause on the CPU tier. It is a
local direct run, not a through-stack measurement of that retrieval
configuration: the 200-document ROSA batch does exercise the specialist
through the deployed stack (§6.2: 600 of 800 extraction calls answered by
`cuad-specialist`), but it
uses the synthetic corpus, not the CUAD top-5 retrieval setup. Error
decomposition over
the 80 extract steps scored (19 documents) at the top-5 window: zero
retrieval misses, zero specialist misses — recall 1.000, so the remaining
errors are extra spans, not missed ones. Scope: that is this scored set, not
a general hit rate. On the separate 157-pair retrieval probe the same
embedder's hit@5 is 125/157 (~80%).

Risk judgment (rubric-following, vs rule-derived truth): the MoE scores
0.632 with real discrimination (16 high / 4 low) under the corrected scorer
(FINDINGS §13); the 1.5B sits at or below the majority-class baseline — it
is not applying the rubric at all. Rubric design matters as much as model
class (a CUAD-specific rule made "always high" optimal; the lesson is baked
into the demo narrative).

### 2b. Direction: contract portfolio intelligence

The measured retrieval results justify extending the scenario from
single-contract review to portfolio queries ("which contracts contain an
audit-rights clause?"): docling ingestion on CPU with PII screening at ingest,
tiered legal embeddings, specialist span extraction, MoE synthesis over
retrieved evidence — every hop through the governed gateway. First
measurements: the MTEB(Law) leaderboard ordering **inverts** on our
within-document task (the 0.6B embedder significantly beats the top-scoring
8B, McNemar p = 0.0075, 125/157 vs 113/157 hit@5) while the 8B wins the
cross-document regime — embedding tiers follow *measured regime fit*, not
the leaderboard. The external method ladder points the same way: on a fixed
CUAD backbone, Standard 72.9 → RAG 79.3 → multi-agent 85.8 F1 **[external]**
— method moves CUAD numbers more than backbone size, and our 0.800-vs-0.690
result is that prediction confirmed on our stack.

### On-stage moments

1. Fan-out / escalation / kill switch: choreographed and working on all
   three stacks (live config swaps through the gateway's config watcher).
2. Pull the plug: GPU pool to zero mid-run; degradation and recovery visible
   in the trace waterfall. **[measured]**
3. Sensitivity pinning: zero violations, verified from the gateway's own
   SQLite audit trail, not the pipeline's say-so. **[measured]**
4. The agent is blocked: sandbox egress denial with policy YAML and live
   denial logs — working on the Linux stack; re-verification on OpenShift is
   on the open list. **[in flight]**

### 2c. Two doors for the same batch — the secure-agent story

The same batch review can be triggered two ways, and the difference *is*
the security story:

|  | Vanilla client | Sandboxed agent (OpenShell) |
|---|---|---|
| What it is | A scripted pipeline — cron job, CI run, notebook: any caller | An autonomous agent (OpenClaw) in a hardened runtime |
| What could go wrong | Misrouting: a sensitive document served by the wrong pool | Misrouting *and* the agent itself: exfiltration or unauthorized actions (including via prompt injection) |
| Where governance lives | With the request — the gateway's label matrix, pinning, fail-closed, audit | With the request **and** the caller — default-deny network, gateway-only egress, every denial logged |
| What it proves | The gateway governs whatever calls it (0 violations, measured) | A contained agent: even a fully compromised agent has a blast radius of zero |

The through-line: the two runs are **indistinguishable at the pool level** —
same labels, same routing decisions, same audit records, because both go
through the same gateway. The gateway never had to trust the caller; the
sandbox means nobody has to. On stage: run the batch vanilla, then move the
same batch into the sandbox, let the agent run it identically, then have it
attempt one unauthorized action — denied, logged, blast radius zero.

Honest edges, stated: the vanilla client is unconstrained *before* the
gateway (a leaky client can still misbehave locally), and the sandbox does
not improve models or routing — it contains the agent. Measurement
discipline: the four arms are measured through the scripted client so every
replay is identical; agent-driven review is the open V2 tool-calling item.

### 2d. The stage UI — a replay view over a scored run

`hybrid-ui` serves one page that walks a recorded run document by document:
the classification card, one card per clause extraction (with the quoted
span located in the source text and marked correct / missed / false
positive / wrong quote), then the risk judgment and the cross-document
report. Stage cards carry their real latency, so the replay animates at
roughly the pace the pipeline actually ran, and a toggle hides all truth so
the audience sees only what the system produced.

```
cd demo
uv run hybrid-ui --run ../runs/cuad-test/arm-direct-moe-gpu-v2 --data data/cuad-test --port 8099
```

Above the cards a strip shows the pipeline as it actually ran — classify,
then the extracts issued together, then risk, with each node carrying the
measured latency the stage recorded. Report is drawn as a node but shown
inactive in a document view: it is computed over the whole run, not this
document, so it belongs to the side panel. During replay the strip fills in
in step with the cards, which makes the fan-out visible: one classify call,
then N extraction calls in flight at once, then risk.

With `--endpoint http://…/v1` the page also offers a live single-document
run: `＋ New document` opens a composer that takes pasted text, a `.txt` or
`.md` file, or a dropped file. The document goes through the same prompts
and parser as the batch runner, with classify and the extracts issued
together (two round trips instead of six), so the live latency on stage is
the interactive latency, not the batch one. The live path is proxied
through the server, so the browser never needs CORS on the vLLM endpoint.

The live run is streamed, not awaited: the server emits one
server-sent event per finished call (`POST /api/live/stream`), and the
strip lights each node as its call returns — classify and the extracts
first, in completion order, then risk once every extract is in. The
audience watches the fan-out happen against the real model rather than
inferring it from the replay. A run against a scored document also shows
a comparison chip, live model time next to that document's recorded model
time, so the two paths can be seen to agree rather than assumed to.

A submitted document has no annotations, so the page marks it `unscored`:
the extracts' quotes are still located in the source and checked against
it — the first thing a reviewer does — but no correctness verdict is
claimed and the field meter stays empty. Text only, because the ingestion
step that turns PDF/DOCX into text is designed, not built; the composer
says so rather than failing silently on a PDF. Submitted documents live in
the server process's memory and are never written to disk.

The same server also serves the presenter's guide at `/guide` (default:
`docs/stage-review-walkthrough.html`, overridable with `--guide`), and the
page's header links to it. Keeping the walkthrough on the same origin as
the page means the two cannot describe different builds. The header chips
show the checkpoint from the run summary — `model` from `--model`, and the
`served_model` alias the endpoint reported — because the OpenAI API exposes
only the served alias, and a run whose checkpoint is unrecorded cannot be
reproduced or compared. `/api/health` reports whether a guide is configured
so a deployment can check both surfaces in one request.

When the server was started with an endpoint, the header carries a second
chip reporting whether that endpoint is reachable and warm: the model name
from `/v1/models` and vLLM's running/waiting request counts from
`/metrics`, polled every 15 s. It is a readiness indicator for the person
driving the demo, not a claim about the pipeline — it says "the box is up
and idle" before the first click, and shows an error instead of a stale
"live" when the endpoint has gone away.

`/monitor` is the same server's telemetry page, and the header links to it.
It has two halves that never mix: live vLLM `/metrics` for each pool named
with `--pool name=url`, and the recorded artifacts beside the runs — the
per-pool counters every through-stack run left in `metrics.json`, and the
benchmark sweep in `runs/cuad-test/perf/`. The recorded half reads only
files, so it works from replay alone with the cluster switched off. vLLM's
token counters are cumulative, so a rate is the change between two polls:
the first poll says "first sample" rather than printing a zero, and a
counter that moves backwards is read as a pool restart and dropped instead
of drawn as a spike. A pool name is a routing tier, not a hardware claim;
the page says so wherever a tier appears, and the sweep's only hardware
label comes from the file name (`cpu-*` is the CPU run).

The UI reports the honest headline alongside the headline: the risk
accuracy bar is drawn against the majority-class baseline ("always answer
*high*" scores 0.684 on this set), and documents fully correct is shown as
a count, not a percentage, because 5 of 19 is the real number.

A run that went through the gateway is a different kind of evidence, and the
page says so per call rather than once at the top. Every served record carries
`x-demo-route`, `x-demo-entry` and `x-praxis-trace-id`; each stage card shows
the pool that answered, the entry that admitted the request and the trace id,
and the side panel's **Where the calls went** block aggregates them by pool and
by entry, alongside the run's own sensitive-step counts (`sensitive_steps`,
`sensitive_on_approved`, `sensitive_violations`, `fallback`). A call that
failed has no route header because nothing answered it; those are counted and
labelled separately, never as an "unrouted" decision. Pool names are routing
tiers from the llm-d convention, not a hardware claim, and the panel says that
too.

Two runs of the same documents can be served together (`--compare <dir>`) and
swapped with an A/B button in the header. Both payloads are built once at
startup, so the toggle costs no I/O; the header always names the run on screen,
because the numbers change with it. This is how a quality run (a direct arm, no
routing) and a governance run (through praxis, most calls failed at concurrency
12) are put side by side without pretending either proves the other's claim.

The side panel also keeps the run honest about its failures and its cost:

- **What it got wrong** lists every extraction verdict that is not correct,
  each row clickable, opening that document's card with the quote and its
  source excerpt. A failed call appears as *no record*, which the scorer counts
  as unfilled — not as a correct "no clause" answer. (`evalkit.score` had
  credited a 502/503/504 on an annotated-absent clause as correct; that is
  fixed, and on `arm-D-specialist` it moved extraction from 53 to 22 correct
  fields and removed one phantom fully-correct document.)
- **Pool load** reads the through-stack `metrics.json` (samples, prompt and
  generation tokens, device-active seconds, scrape method) and shows tokens
  per *active device second*, not per wall second, so an idle pool is not
  credited with throughput.
- **Throughput by concurrency** renders the `vllm bench serve` sweeps found in
  `runs/cuad-test/perf/`, one table per hardware and prompt set at
  concurrency 1, 4 and 16: TTFT p50/p95, TPOT, output tokens/s and failures.
  The same prompts run on the CPU host and the GPU host, which is the only
  place the page compares hardware directly.
- **What we sent** is a collapsed disclosure on each stage card holding the
  request body's model, token cap, temperature and messages, clipped to keep
  the page light. It answers "what exactly did you ask it" without a network
  capture.

A run's manifest can list the same truncated `doc_id` twice, and the batch
runner then executes that contract twice — 20 entries for 19 contracts on
`cuad-test`. The page keeps the first pass, shows one card per contract, and
says `1 repeated doc_id folded` in the header, so its field and call counts
are deliberately smaller than the run's own `summary.json` rather than
double-counting one contract.

Live mode also takes a corpus. The composer accepts several `.txt`/`.md`
files or one `.zip`/`.tar`/`.tar.gz`/`.tgz` archive, and `POST
/api/corpus/stream` runs the batch two documents at a time, streaming
`corpus_start`, `doc_start`, `stage`, `doc_done`, `doc_error` and `done` events
so each document's row fills in as its calls land. The archive is read from
memory — never extracted to disk — with absolute paths, `..` members, symlinks
and devices rejected, each member size capped before it is read, binary
content refused even under a `.txt` name, and the caps enforced at 64 documents
/ 400,000 characters each / 8,000,000 in total. Skipped members are named in
the event stream, so the presenter can say what was ignored.

## 3. Architecture (as built)

![As-built architecture: clients (pipeline, sandboxed agent) → trace-ui / praxis gateway → llm-d-sc classification → pools; the dashed green boundary marks the approved set, inside which sensitive data is processed](architecture-as-built.svg)

*(Editable source: `docs/architecture-as-built.svg`.)*

- **Praxis** is the gateway (V13 resolved); **llm-d-sc** supplies
  complexity labels (the pipeline's PII screen is what picks the
  sensitive entry — two labels in one routing chain remains open, V19);
  four routing configurations implement the
  measurement arms; the same configs drive Linux and OpenShift byte-identically.
- Pools are plain vLLM Deployments on OpenShift (KServe's storage-initializer
  cannot reach HuggingFace from this cluster — documented, worked around;
  the ROSA overlay uses per-node gp3 model caches seeded offline instead).
- The **CUAD specialist** is a composite pool: extraction prompts answered by
  the fine-tuned RoBERTa-large QA checkpoint in one encoder pass; everything
  else forwarded to the general model behind the same relay. An opt-in
  semantic cache with the agreed ordering rules is available (measured on
  the Linux stack — zero-FP threshold 0.98; off in the OCP overlay as
  deployed: exact-match always safe, similarity serving never stores or
  serves sensitive content).

**Models actually used** (replacing v0.1's candidates, which were never
touched): CPU tier — Qwen2.5-1.5B and Qwen3.5-35B-A3B MoE (vLLM, Xeon,
16k context); GPU tier — Qwen2.5-7B (g5); specialist — RoBERTa-large CUAD
checkpoint (Zenodo); embeddings — litil-embed-0.6b / octen-law-8b-v1
(measurement-selected). Licences verified (V12): Apache-2.0 models, CC BY 4.0
dataset, no customer data anywhere.

**GPU pool overlay.** `deploy/openshift/gpu-moe` stacks on `cpu-real-moe` and
serves the same Qwen3.5-35B-A3B checkpoint on 4× A10G (TP=4, 16k context)
behind `vllm-gpu` and `vllm-gpu-approved`, leaving `vllm-cpu` on CPU. One
checkpoint on two hardwares is the controlled comparison: identical quality
(F1 0.690, risk 0.632, 5/19 fully correct on both) with classify at 3.4–21.3 s
on CPU against ~0.6–1 s on four A10Gs. The overlay parks the CPU-served 7B
pools it replaces, and requires the `model-cache` PVC to be seeded first
(`HF_HUB_OFFLINE=1`). The approved Service selects the same pods by default:
that promises sensitive traffic lands on the GPU pool, not on isolated
hardware — a second Deployment costs another four GPUs.

**Demo target — ROSA HCP (2026-11-04).** `deploy/openshift/rosa` composes the
same base onto a managed hosted-control-plane cluster with the CPU and GPU
nodes colocated in **one AZ**: an `x8i.24xlarge` CPU node (48 physical cores,
2 NUMA nodes × 24, AMX bf16/int8) and a `g5.12xlarge` GPU node (4× A10G).
Both pools are tainted so only the demo workloads land there. There is **no
static CPU manager and no PerformanceProfile**: each pool pins its whole
process tree to an explicit, NUMA-local cpuset with `taskset -c` and
requests CPU *without* a limit, so the CFS does not throttle a bound pool.
(`VLLM_CPU_OMP_THREADS_BIND` + `libiomp5` are still set for intent, but in
this image the engine forks after the preloaded runtime initialises and the
env-only path applies nothing — measured 2026-11-04: 234/234 threads on
`0-95`; `taskset` then gave 126/126 on `0-15`.) Measured lists on the x8i:
MoE `0–15` (NUMA 0), specialist `16–17`, 1.5B `24–35` (NUMA 1), parked 7B
`36–47`; `18–23` stay free for housekeeping. Weights live on per-node gp3
PVCs seeded before the pools start (`HF_HUB_OFFLINE=1`). The bind lists are
regenerated from the real node (`scripts/cluster/cpu-topology.sh`), never
inferred from the instance type. This environment is the demo; its numbers
are the re-baseline in §6, not the SNO figures above.

**Run provenance.** Both runners record `--environment` and `--cluster` (or
`HYBRID_ENVIRONMENT`/`HYBRID_CLUSTER`), the git commit and whether the tree
was dirty, concurrency, timeout, and `--pools` — a JSON map of route →
model/hardware/deployment — in `summary.json`; the stage UI renders it as a
`where` chip. Route and label headers say which pool answered; they cannot say
what was in the pool, and only the operator knows that at deploy time.

## 4. Phased plan — status

| Phase | Exit criteria (v0.1) | Status |
|---|---|---|
| 1: agent + gateway + multi-replica vLLM | pipeline end-to-end on both pools; baseline captured | **Done** — full-platform validation on OpenShift (4.22 SNO), then re-baselined on the ROSA demo stack with both CPU and GPU halves measured (§6.1–6.3) |
| 2: EPP + semantic layer + dashboard | measured improvement vs round-robin and all-GPU; quality within tolerance; survives GPU removal | **Done** — four arms measured on ROSA (200 docs each): arm D 54× less GPU time than all-GPU (0.036 vs 1.974 GPU-s/doc), extract F1 0.982 vs 1.0, risk 0.995, 0 sensitive violations; 45× on the earlier g5 validation; degradation verified; live dashboard + audit export |
| 3: OpenShell | sandboxed pipeline; denied action demonstrated | **Done** on Linux; OpenShift sandbox deployed, blocked-action moment pending re-verification |

## 5. Measurement design (as run)

Same documents, same replay, same scorer for every arm; CUAD held-out test
split for all quoted numbers (a mixed split containing train documents read
optimistically and is no longer quoted); contamination discipline for the
fine-tuned specialist (test.json only); governance verified from the
gateway's own records, independently of the pipeline.

**Baseline and re-baseline.** The ROSA demo stack changes the hardware, the
CPU allocation technique and, for the demo, the document set: a 200-document
synthetic corpus with 41 sensitive documents, plus explicit 1–2 document
demo runs. Direct and stacked are measured as separate instruments:

| run | documents | instrument | what it answers |
|---|---|---|---|
| live demo | 1–2 (`--doc-ids`, one sensitive) | arm D | containment visible in the trace |
| batch | 200 synthetic (41 sensitive) | arms A–D | quality, routing, zero-violation at scale |
| direct | 200 synthetic + 19 CUAD-test | `hybrid-direct-arm` per pool | single-model quality baseline |
| stacked | 200 synthetic | arm D + specialist | is the composite worth it |

`--docs N` alone prefers the manifest's golden set, so the first document is
non-sensitive; explicit `--doc-ids` is required to put a sensitive contract
on stage. The report splits quality and routes by the dataset's own
`sensitive` label — CUAD carries none, so that split is synthetic-only and
the report says so rather than folding unlabelled documents into
"non-sensitive". **The demo numbers are §6.2 and §6.3**; the SNO/CUAD table
below is the earlier validation record and is superseded for anything quoted
on stage.

The SNO/CUAD validation record (not the demo number set):

| Arm | Description | Result **[measured]** |
|---|---|---|
| A | All-GPU, large model | 25/25 sensitive violations; 9.35 GPU-s/doc; F1 0.690 (corrected scorer) |
| B | All-GPU, small model for simple steps | 25/25 violations — the skeptic's alternative measured and beaten (V18) |
| C | Hybrid, round-robin | 12–24 violations — unclassified hybrid is not governance |
| **D** | **Classifier + two-entry matrix + fail-closed** | **0 violations, every stack, every dataset; ~99% CPU-served; 0.21 GPU-s/doc** |

**The two paths behind those numbers.** They are different instruments and
neither proves the other's claim: the direct arm sets the model-quality
reference with nothing in the path; the through-stack arms show what the
gateway does to a request, and their records are the governance evidence.

![Direct run: the batch runner calls a vLLM endpoint directly — no relay, gateway, label routing or trace](pipeline-direct.svg)

![Through-stack run: batch runner → trace-ui relay → praxis gateway → pool → model; the approved set is where sensitive steps are served](pipeline-through-stack.svg)

Cost discipline held: device-time measured exactly (simulated counters +
live /metrics integration), priced only as a sweep (cloud list vs owned at
25/50/75% utilisation). No single $/document figure is claimed beyond the
labelled $0.01-vs-$0.57 calculation. A validated organisational cost model
remains open (V17).

## 6. Validate first — results

Ordered as in v0.1. This table is the audit trail — the plain-language
summary of what these checks mean is in "Results in plain language" above.
"Open" items are the honest remainder.

| # | Item (v0.1) | Result |
|---|---|---|
| V1 | Agent prompt size vs CPU prefill/TTFT | Partially retired: workload is batch; long-context prefill measured as the ~50% CUAD slowdown vs synthetic; context capped at 16k. TTFT/concurrency sweep (hybrid-bench) **open** |
| V2 | Tool calling through the full stack | **Open** |
| V3 | CPU model loads and runs on our hardware | **Validated** — 1.5B/7B/9B/35B-A3B served by vLLM on Xeon; plain builds, no vendor tuning needed |
| V4 | Routing by model name; prefix caching on CPU | **Validated** via Praxis label routing (architecture evolved past plain EPP); prefix-cache engagement not separately isolated |
| V5 | Semantic cache hit/FP rates; threshold | **Validated** — threshold sweep: zero-FP operating point 0.98 with 41.7% hits; similarity layer awaits a real embedder (litil is the measured swap-in) |
| V6 | Fan-out limits; 200-document visual | Concurrency 12 stable; the 200-document scale run is **ready but not yet run** on ROSA (200-document synthetic set, 41 sensitive, arms A–D + direct) |
| V7 | Failover: kill the GPU pool mid-run | **Validated** — degradation, fail-closed, isolation, recovery, all stacks |
| V8 | Quality harness + acceptance threshold | **Validated** — evalkit scorer vs CUAD ground truth; every arm compared to Arm A on identical fields |
| V9 | OpenShell on OpenShift | **Validated** — Helm install, SCC and CRDs handled; runbook scripted |
| V10 | Sandbox → gateway path | **Validated** — in-cluster plain-HTTP policy proxy; egress praxis-only, default-deny, logged |
| V11 | Observability plumbing | **Validated** — trace waterfall, arms panel, dashboard, metrics sampler, JSONL audit export |
| V12 | Licences | **Validated** — Apache-2.0 models; CUAD CC BY 4.0 (confirmed on the Atticus site); no customer data |
| V13 | Define the "Praxis gateway" | **Resolved** — Praxis is the gateway; llm-d-sc the classifier; four config-driven arms |
| V14 | Classifier transfer to contract steps | **Validated with a twist** — llm-d-sc labels all contract-analysis steps MEDIUM, only cross-doc reports REASONING; within-tier composition lives in the composite pool; prompt wording calibrated so report prompts label REASONING; disagreement reported, never hidden |
| V15 | Sensitivity enforcement beyond the classifier | **Validated** — PII screen (P 1.0 / R 1.0 synthetic; 5–12/20 CUAD docs flagged), NetworkPolicies, and gateway-record verification; two-label routing in a single chain remains **open** (V19) |
| V16 | GPU-removal for in-flight REASONING | **Validated** — bounded degradation with visible badge; fail-closed for sensitive; recovery verified |
| V17 | Cost model agreement | **Partial** — device-time exact; priced as sweep only; org-validated rates open |
| V18 | Skeptic's baseline (Arm B) | **Validated** — measured and beaten. The 7B was never run on the held-out split, so the old mixed-split comparison is retired; on identical test-split fields the CPU MoE scores 0.690 and the retrieval-first specialist 0.800 (local direct) |
| V19 | Both labels routed at once | **Open** — implemented as two gateway entries (general/sensitive); single-chain two-label routing unvalidated |

**Also open:** thinking-mode A/B (Qwen3.5 thinking is the untested quality
lever for the judgment tier); the 7B on the held-out test split (headline
hygiene); ContractNLI as the risk-tier evaluation; docling ingestion
(designed; scoped as a CPU-side ingest service with its own core budget —
PII screen at ingest — never inside the vLLM pool pods); the embedding-tier
regime decision (cross-document corrections and the dinghy-8B evaluation are
**[in flight]** as of this draft). The specialist through the full stack is no
longer open: 600 of 800 arm-D extraction calls were answered by
`cuad-specialist` (§6.2, FINDINGS §15.4).

### 6.1 ROSA demo re-baseline (2026-11-04)

| item | state |
|---|---|
| Cluster | ROSA HCP `<redacted-cluster>` (us-east-1a), managed control plane, CPU and GPU nodes colocated in one AZ. `<redacted-cluster-eu>` (eu-west-1) was built and torn down the same day: AWS had no `g5.12xlarge` capacity in any region we could use. The `<redacted-cluster>` pools (`<redacted-gpu-pool>`, `<redacted-cpu-pool>`) were created and both nodes were Ready for the measurement window (torn down after the runs, see below). |
| CPU node | `x8i.24xlarge` (`<redacted-node-3>`): topology re-read on this node — identical to the recorded layout (48 physical cores, 2 NUMA nodes × 24, siblings `(i, i+48)`, AMX bf16/int8; `problems: []`). Pinned with `taskset`: 126/126 engine threads on `0-15`, 1.5B on `24-35` (verified in `/proc`). Per-node gp3 cache seeded; MoE and 1.5B serving. |
| GPU node | `g5.12xlarge` (`<redacted-node-4>`): Ready, 4 GPUs allocatable, GPU operator healthy; `gpumoe-vllm` serving TP=4 from the seeded `model-cache-gpu`. |
| Overlay | `deploy/openshift/rosa`: base + KServe-free pools, `taskset` cpusets, CPU request without limit, startup probes, per-node caches, GPU pools tainted. Two fixes from the live bring-up: `VLLM_CPU_KVCACHE_SPACE=16` + 192 GiB limit (the 67 GiB checkpoint's page cache broke the auto KV calc at 128 GiB), and `taskset` as the effective pin (the OMP env path logs but does not apply in this image). |
| Tooling | 200-document synthetic set (41 sensitive), `--doc-ids` for the 1–2 document demo, sensitivity split in the report, `scripts/cluster/direct.sh` for the direct arm. |
| Smoke evidence | 1 non-sensitive + 1 sensitive document through arm D: **0 violations**, 7/7 sensitive steps on approved pools, extract F1 1.0, risk 1.0, 2/2 fully correct, 26.8 s. |
| Batch done | 200-document matrix (arms A–D) complete — see §6.2. Headline: 0 violations, 99.9% CPU-served under a CPU-first policy, 54× less GPU time. |
| Direct done | 200 documents against three bare endpoints (`cpu-moe`, `gpu-moe`, `cpu-small`), scored against the same ground truth — see §6.3. |
| Not yet run | the OpenShift sandbox blocked-action re-verification. No sandbox workload or manifests live in this repository (OpenShell/OpenClaw are external), so this remains the one unverified demo moment. |
| Teardown | After the measurements were copied, `openshell-demo` was deleted with the namespace and both `<redacted-pool-*>` machine pools were removed; the instances terminated and their ASGs are gone. The shared cluster and its other pools were left unchanged. |

**Classifier caveat.** On the smoke pair llm-d-sc labelled 12 of 13 steps
MEDIUM (accuracy 0.077 against the synthetic truth). The report prints the
caveat: at that accuracy the ~92% CPU-served share reflects the CPU-first
policy, not demonstrated complexity separation. The 200-document replay
(§6.2) shows this is the classifier's prompt-difficulty taxonomy, not
degradation: quote the two-level split (single-document → CPU,
cross-document synthesis → GPU), not a four-way accuracy.

### 6.2 ROSA measurements — 200-document synthetic matrix (2026-11-04)

`<redacted-cluster>`, arms A–D, 200 documents each (41 sensitive), concurrency
12, real vLLM on x8i + 4× A10G. Raw records: `runs/rosa-200`.

| arm | wall | extract F1 | risk acc | fully correct | CPU-served | sensitive steps on approved | violations | GPU-s/doc |
|---|---|---|---|---|---|---|---|---|
| A all-GPU | 202 s | 1.000 | 1.000 | 200/200 | 0.0% | 0/247 | 247 | 1.974 |
| B GPU-small (1.5B) | 439 s | 1.000 | 0.395 | 77/200 | 0.0% | 0/247 | 247 | 2.218 |
| C round-robin | 613 s | 0.992 | 1.000 | 188/200 | 50.0% | 125/247 | 122 | 2.855 |
| **D matrix** | **1112 s** | **0.982** | **0.995** | **173/200** | **99.9%** | **247/247** | **0** | **0.036** |

This is a **CPU-first policy**, not demonstrated complexity routing: classify,
extraction and risk all ran on the CPU (600 extractions answered by
`cuad-specialist`; the rest forwarded by the specialist to the CPU MoE), and the
GPU served only the single cross-document report. The 99.9% CPU share is the
policy; see the classifier caveat below.

Sensitive vs non-sensitive, arm D (labels from the dataset, not the screen):

| class | docs | extract F1 | risk acc | fully correct | routes |
|---|---|---|---|---|---|
| non-sensitive | 159 | 0.979 | 0.994 | 135/159 | vllm-cpu:954 |
| sensitive | 41 | 0.990 | 1.000 | 38/41 | vllm-cpu:246 |

(The governance counter totals 247 sensitive steps: it includes one
document-level step beyond the 246 per-class steps. The single approved-GPU
route in arm D is that extra step, which is also the one step the classifier
labelled REASONING.)

Reading:

- **Governance is the headline.** D: 0 violations, 247/247 sensitive steps
  on approved pools. C (unclassified round-robin) leaks 122/247. A and B
  violate by construction (247/247). The screen itself was perfect on this
  dataset (41 tp, 0 fp, 159 tn, 0 fn) in all four arms.
- **Quality.** D loses 1.8 F1 points and 0.5 risk points vs all-GPU, and its
  report accuracy is slightly *higher* (0.764 vs 0.756) — within the agreed
  tolerance at 173/200 fully correct. The 1.5B arm B collapses on risk
  (0.395, below the 0.485 majority baseline; 77/200): "CPU" is not the same
  as "small". The 35B-A3B on CPU does the judgment step; the 1.5B does not.
- **The specialist was in the path, not a side experiment.** Every
  extraction record names its responder: of 800 extraction calls,
  **600 were answered by `cuad-specialist`** (governing law, liability
  cap, termination) and 200 (data protection) by the CPU MoE. All routed
  through the composite (`x-demo-route=vllm-cpu`). Derivation:
  `runs/rosa-200/specialist-metrics-2026-11-04.txt`. That is the
  through-stack evidence the earlier 0.800 local run lacked.
- **Cost.** 54× less GPU time: 0.036 vs 1.974 GPU-s/doc, 7.3 vs 394.8
  GPU-seconds total. At the illustrative $1.10/GPU-hour that is ≈ $0.002 of
  GPU time vs ≈ $0.12 for the batch (GPU time only; CPU core-time is
  unmeasured, see below).
- **Speed.** D is 5.5× slower end-to-end (1112 s vs 202 s). This is a batch
  trade-off, not a latency claim.
- **Classifier caveat — it is a taxonomy mismatch, not a broken model.**
  The 0.001 "accuracy" compares the classifier's prompt-difficulty taxonomy
  with the generator's per-step labels; that is a category error. Replaying
  the real arm-D prompts through the image's own `llm-d-sc-classify` gives a
  clean, reproducible picture:

  | step prompt | MEDIUM | SIMPLE | COMPLEX | REASONING | routed |
  |---|---|---|---|---|---|
  | classify contract | 0.992 | 0.024 | −0.101 | −0.384 | CPU |
  | extract termination | 0.993 | 0.002 | −0.092 | −0.377 | CPU |
  | assess risk | 0.991 | −0.000 | −0.112 | −0.351 | CPU |
  | portfolio report (37k ch) | 0.398 | −0.496 | −0.392 | **0.707** | approved GPU |

  The working boundary is **single-document analysis → CPU** vs
  **cross-document synthesis → GPU**, which is the boundary the policy needs.
  The classifier never emits SIMPLE/COMPLEX on this workload, and no exposed
  knob changes that: the only server setting is the taxonomy name
  (`complexity`/`cost`/`sensitivity`), and praxis's `min_score` cannot help
  when the margins are 0.3–0.99. Report the confusion matrix as *agreement
  with step-type labels*, not accuracy; arm D's CPU share is the CPU-first
  policy plus this two-level split, not four-way complexity routing.
- **Measurement gap.** CPU core-s/doc is not reported: the relay-fronted CPU
  pool (`cpureal-specialist` composite) does not expose vLLM's in-flight
  gauges, so the sampler returns null. The report now prints `n/a` (not
  zero) and withholds the dollar sweep for arms C/D; GPU time is measured
  and is the decisive number. CPU core-time needs a direct scrape of
  `cpureal-vllm-cpu` in a follow-up run.
- **Scope.** These are synthetic documents: extraction is a generated copy
  task (F1 1.0 for A/B), so this matrix measures governance, routing and
  scale, not model quality. The CUAD test-split numbers (§2) remain the
  quality benchmark.

### 6.3 Direct baselines — one bare endpoint per arm (2026-11-04)

Same 200 documents and the same scorer, with no router: each arm sends its
1,201 requests (classify + six extractions + risk per document, plus one
portfolio report) to a single pool. Raw records:
`runs/direct/{cpu-moe,gpu-moe,cpu-small}`; combined report:
`runs/rosa-direct-vs-stacked/report.md`.

| arm | pool | wall | extract F1 | risk acc | report acc | fully correct | errors |
|---|---|---|---|---|---|---|---|
| cpu-moe | 35B-A3B, 16 CPU cores | 2095 s | 1.000 | 1.000 | 0.746 | 200/200 | 0 |
| gpu-moe | 35B-A3B, 4× A10G TP=4 | 228 s | 1.000 | 1.000 | 0.768 | 200/200 | 0 |
| cpu-small | 1.5B, 12 CPU cores | 594 s | 1.000 | 0.395 | 0.212 | 77/200 | 0 |
| **D stacked** | mixed, governed | **1112 s** | 0.982 | 0.995 | 0.764 | 173/200 | 0 |

Reading:

- **The router is not the cost.** Direct GPU (228 s) and the stacked all-GPU
  arm A (202 s) run the same 1,201 requests in effectively the same time:
  governance, classification and routing add no meaningful wall-clock
  overhead.
- **All-CPU is 9.2× slower than all-GPU** (2095 s vs 228 s) and 1.9× slower
  than arm D (1112 s). The specialist explains the difference: 600 of 800
  extraction calls leave the MoE for the 2-core CUAD model in the stacked
  arm, while the direct arm keeps every call on the 16-core MoE.
- **The F1 cost is the specialist, not the device.** Direct CPU and direct GPU
  both score 1.000; D scores 0.982 because `cuad-specialist` answers 600
  extractions. On this task, CPU-vs-GPU is not an extraction-quality
  trade-off.
- **Report quality tracks the GPU.** D's report accuracy (0.764) is within
  0.004 of direct GPU (0.768) and 0.018 above direct CPU (0.746), consistent
  with the report step being GPU-served in D.
- **"Small" is the failure mode, not "CPU".** Direct `cpu-small` collapses on
  risk (0.395) and report (0.212) exactly like stacked arm B, which runs the
  same 1.5B weights on GPU. The model, not the device, sets judgment quality.
- **Direct arms are ungoverned by construction.** All 247 sensitive steps go
  to the single pool: there is no screen and no approved-pool set, so the
  report prints `n/a` (not 0) for their routing cells. D is the only arm that
  is both fast and compliant: 0 violations, 247/247 approved, 0.036
  GPU-s/doc.
- **Direct device time is unmeasured.** The direct runs did not sample
  `/metrics`, so their GPU/CPU seconds are `n/a` and no dollar figure is
  quoted; wall-clock is the comparison. The stacked arms' GPU time remains
  the measured cost number.

Caveat: the direct arm used concurrency 8 (the stacked matrix used 12), so
wall-clock ratios are indicative, not a controlled throughput benchmark.

## 7. Documents and data — as used

CUAD v1 (CC BY 4.0, confirmed): 510 expert-annotated commercial contracts;
held-out **test split** for every quoted number; 41 clause categories with
4 mapped to the demo scenario. The golden-set practice from v0.1 became the
19-unique-contract held-out set (the manifest carries 20 entries; one
`doc_id` collision) with recorded evidence per run. Annotation noise is
measured and stated (precision is a lower bound for all models; comparisons
remain fair on identical annotations). CUAD has no data-protection category —
the DP clause is exercised with synthetic PII documents instead, which also
drives the sensitivity screen.

## 8. Risks — revisited

| Risk (v0.1) | Outcome |
|---|---|
| CPU latency compounds across sequential steps | Retired by measurement: the workload is batch; "same quality, different speed" is the degradation story, not the steady-state pitch |
| OpenShell alpha / experimental Kubernetes path | Worked on OpenShift 4.22 with documented SCC/Helm overrides; still presented as demo-cluster, not production |
| Privileged SCC concerns | Handled upfront; sandbox SCC scoped and documented |
| Semantic cache returns wrong answers | Zero-FP threshold measured (0.98); FP rate shown honestly per arm |
| Vendor-tuned builds may not run | Moot — plain vLLM builds served every model on Xeon |
| Demo depends on unmeasured numbers | No longer: every on-stage number traces to a recorded run |

New risks worth naming: **annotation noise is the quality ceiling** (CUAD's
SOTA ≈ 88.3 F1 **[external]**; our 0.800 sits against that bound); the
cluster environment is shared (our OpenShift namespace was found emptied once
— all evidence is local and the stack is redeployable from runbooks); and
**GPU capacity is the deployment critical path, not a measurement gap**: AWS
showed zero `g5.12xlarge` capacity in eu-west-1 on 2026-11-04 (`<redacted-cluster-eu>` was
torn down for that reason), so the demo runs on the managed `<redacted-cluster>`
cluster in us-east-1, where both pools are Ready and the full matrix has
since been measured (§6.2). The remaining constraint is pool cost/uptime, not
access to the hardware. Single-run measurements are labelled as such and
significance is computed where per-pair data exists.

## 9. References

- Source repository: demo code, runbooks, evidence packs (`runs*/`),
  and the full measurement record (`FINDINGS.md`) — the primary reference.
- CUAD: the Atticus Project (CC BY 4.0).
- Praxis gateway; llm-d-sc semantic classifier; vLLM; OpenShell / OpenClaw.
- External CUAD method ladder and SOTA: see FINDINGS §3.5 (aggregators —
  verify against primary papers before external publication).
