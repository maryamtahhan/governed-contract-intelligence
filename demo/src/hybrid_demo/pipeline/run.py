"""One entry point for both pipeline transports.

The prompt builders, parser, ground truth and scorer are already shared in
``review.py``; the only real difference between a direct run and a
through-stack run is the target the batch runner posts to. This command makes
that choice explicit instead of requiring two entry-point names:

    hybrid-run --target direct  --endpoint http://host:8000/v1 \
        --data data/cuad-test --out ../runs/direct/gpu-moe

    hybrid-run --target gateway --gateway http://relay:8080 \
        --sensitive-gateway http://relay:8083 \
        --judgment-gateway http://relay:8086 \
        --sensitive-judgment-gateway http://relay:8087 \
        --config hybrid --data data/synthetic --out ../runs/rosa-200/hybrid

The older ``hybrid-direct-arm`` and ``hybrid-review`` commands still work and
remain the low-level entry points.
"""
from __future__ import annotations

import argparse
import sys

from . import direct, review


def _target_from(raw: list[str]) -> str | None:
    for i, value in enumerate(raw):
        if value == "--target" and i + 1 < len(raw):
            return raw[i + 1]
        if value.startswith("--target="):
            return value.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Show the selected runner's real options instead of this wrapper's.
    if any(value in ("-h", "--help") for value in raw):
        target = _target_from(raw)
        if target == "direct":
            return direct.main(["--help"])
        if target == "gateway":
            return review.main(["--help"])
    parser = argparse.ArgumentParser(
        description="run the contract-review pipeline against one target",
        epilog="Targets: direct = one vLLM endpoint; gateway = trace-ui relay + "
               "praxis. Options after --target go to that runner: --endpoint for "
               "direct; --gateway, --sensitive-gateway, --judgment-gateway, "
               "--sensitive-judgment-gateway and --config for gateway. "
               "Config names describe routing, not hardware.",
    )
    parser.add_argument("--target", required=True, choices=("direct", "gateway"),
                        help="where model calls go: one endpoint, or the governance stack")
    known, rest = parser.parse_known_args(raw)
    if known.target == "direct":
        return direct.main(rest)
    return review.main(rest)


if __name__ == "__main__":
    main()
