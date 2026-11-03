"""Paired significance test for two retrieval runs.

Both runs must cover the same (document, clause) pairs and persist the
first-hit rank per pair (``per_pair_first_hit`` in the retrieval evaluator's
JSON). A pair is a "hit" when its first hit rank is <= --k. McNemar's exact
test compares the two discordant cells: pairs only run A hit and pairs only
run B hit. Two-sided p-value is the exact binomial tail, not the chi-square
approximation, because the discordant count is often small.

    cd demo && uv run python tools/mcnemar.py \
        runs/retrieval/litil-embed-0.6b--baseline-perpair.json \
        runs/retrieval/octen-law-8b-v1--baseline-perpair.json --k 5
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def load_hits(path: str, k: int) -> dict[str, bool]:
    data = json.loads(Path(path).read_text())
    ranks = data.get("per_pair_first_hit")
    if not isinstance(ranks, dict) or not ranks:
        raise SystemExit(f"{path}: no per_pair_first_hit map")
    return {key: rank is not None and rank <= k for key, rank in ranks.items()}


def exact_mcnemar(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for discordant counts b and c."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2.0 * tail)


def compare(a: dict[str, bool], b: dict[str, bool], k: int) -> dict:
    shared = sorted(set(a) & set(b))
    if not shared:
        raise SystemExit("the two runs share no pairs")
    only_a = sum(1 for key in shared if a[key] and not b[key])
    only_b = sum(1 for key in shared if b[key] and not a[key])
    both = sum(1 for key in shared if a[key] and b[key])
    neither = len(shared) - only_a - only_b - both
    return {
        "k": k,
        "pairs": len(shared),
        "excluded": len(set(a) ^ set(b)),
        "hit_a": only_a + both,
        "hit_b": only_b + both,
        "both": both,
        "only_a": only_a,
        "only_b": only_b,
        "neither": neither,
        "p_value": exact_mcnemar(only_a, only_b),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_a")
    parser.add_argument("run_b")
    parser.add_argument("--k", type=int, default=5,
                        help="rank threshold that counts as a hit (default 5)")
    args = parser.parse_args()

    result = compare(load_hits(args.run_a, args.k), load_hits(args.run_b, args.k), args.k)
    print(json.dumps(result, indent=2))
    print(f"hit@{args.k}: A={result['hit_a']}/{result['pairs']} "
          f"B={result['hit_b']}/{result['pairs']}  "
          f"discordant A-only={result['only_a']} B-only={result['only_b']}  "
          f"p={result['p_value']:.4f}")


if __name__ == "__main__":
    main()
