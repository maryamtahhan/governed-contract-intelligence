# Retrieval evaluation — CUAD test split

Evidence pack for FINDINGS §11. Two protocols, all single runs on Apple
Silicon (2026-10-31), cosine similarity on normalized embeddings, each
model's own passage-side prompt (`passage`/`document`).

## Within-document retrieval (what retrieve-then-extract feeds the extractor)

For every (document, clause-category) pair in the held-out test split, the
official CUAD question is issued as a retrieval query over that document's
chunks. A hit at rank k = the retrieved chunk overlaps the expert-annotated
answer span. 20 documents (400–24,000 chars), 157 pairs.

Files (the parenthetical is the query-side instruction):
- `<model>.json` — baseline: each model's own generic web-search `query`
  prompt
- `<model>--clause-prompt.json` — clause-finding instruction, identical
  text applied to every model
- `litil-embed-0.6b--legalquad-prompt.json` — litil's own LegalQuAD prompt

Reproduce (embedders cached in the local HF cache):

    cd demo && uv run --extra retrieval hybrid-retrieval-eval \
        --model litillabs/litil-embed-0.6b --query-prompt clause

The prompt-A/B runs persist the first-hit rank per pair
(`per_pair_first_hit`) for paired significance tests; `*--baseline-perpair.json`
are verification reruns of the original baselines with that field added —
numbers identical to the originals (determinism check). All runs on the
local Mac (Apple Silicon, MPS); wall-times there are not serving numbers.

## Cross-document portfolio retrieval (the MTEB(Law) regime)

The full test split as one index: 102 documents, 4,004 chunks, 40 clause
categories with gold documents. Per category, the official question ranks
documents by their best-scoring chunk ("which contracts contain an
audit-rights clause?"); gold = documents with an expert-annotated span for
that category. Metrics: MRR (first gold document), success@k (at least one
gold document in the top k), macro doc-recall@10 — read MRR/success first:
recall@10 is bounded by gold-set sizes and some categories have 30+ gold
documents.

Files: `<model>--crossdoc.json` (each model's own query prompt).

Reproduce:

    cd demo && uv run --extra retrieval hybrid-retrieval-eval \
        --model litillabs/litil-embed-0.6b --mode cross --max-chars 1000000

Results and caveats: FINDINGS.md §11.
