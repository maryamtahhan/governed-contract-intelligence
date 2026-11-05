"""Paired significance test for two retrieval runs.

Both runs must cover the same (document, clause) pairs and persist the
first-hit rank per pair (``per_pair_first_hit`` in the retrieval evaluator's
JSON). A pair is a "hit" when its first hit rank is <= --k.

The math now lives in ``hybrid_demo.evalkit.stats``: the exact binomial tail
is the dependency-free default, and statsmodels' exact McNemar is used when
installed (the backend is recorded in the output).

    cd demo && uv run python tools/mcnemar.py \
        runs/retrieval/litil-embed-0.6b--baseline-perpair.json \
        runs/retrieval/octen-law-8b-v1--baseline-perpair.json --k 5
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hybrid_demo.evalkit.stats import compare_hits


def load_hits(path: str, k: int) -> dict[str, bool]:
    data = json.loads(Path(path).read_text())
    ranks = data.get("per_pair_first_hit")
    if not isinstance(ranks, dict) or not ranks:
        raise SystemExit(f"{path}: no per_pair_first_hit map")
    return {key: rank is not None and rank <= k for key, rank in ranks.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_a")
    parser.add_argument("run_b")
    parser.add_argument("--k", type=int, default=5,
                        help="rank threshold that counts as a hit (default 5)")
    parser.add_argument("--backend", choices=("auto", "statsmodels", "exact"),
                        default="auto")
    args = parser.parse_args()

    result = compare_hits(load_hits(args.run_a, args.k), load_hits(args.run_b, args.k),
                          args.k, backend=args.backend)
    print(json.dumps(result, indent=2))
    print(f"hit@{args.k}: A={result['hit_a']}/{result['pairs']} "
          f"B={result['hit_b']}/{result['pairs']}  "
          f"discordant A-only={result['only_a']} B-only={result['only_b']}  "
          f"p={result['p_value']:.4f} ({result['backend']})")


if __name__ == "__main__":
    main()
