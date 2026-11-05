"""Concurrency/throughput benchmarks using the serving stack's own tooling.

``tools/gpu_bench.py`` hand-rolls a client that measures TTFT and throughput.
vLLM ships ``vllm bench serve`` and GuideLLM is the maintained load generator
used by the llm-d/OpenShift ecosystem, so the demo should call those and keep
its own code for the *routing* measurements (which arm served what, at which
tier) rather than for load generation.

This module only builds and runs commands; it does not parse their output, so
it cannot drift when their report format changes.  Use ``--dry-run`` in CI to
assert the flags, and ``--run`` on the real stack.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess


def vllm_command(base_url: str, model: str, *, dataset: str = "random",
                 num_prompts: int = 200, request_rate: float = 4.0,
                 max_concurrency: int | None = None,
                 output_json: str | None = None,
                 extra: list[str] | None = None) -> list[str]:
    command = ["vllm", "bench", "serve", "--backend", "openai-chat",
               "--base-url", base_url, "--model", model,
               "--dataset-name", dataset, "--num-prompts", str(num_prompts),
               "--request-rate", str(request_rate)]
    if max_concurrency is not None:
        command += ["--max-concurrency", str(max_concurrency)]
    if output_json:
        # Current vLLM spells this --save-result + --result-dir/--result-filename;
        # the older --output-json flag is gone.
        from pathlib import Path

        result = Path(output_json)
        command += ["--save-result", "--result-dir", str(result.parent or "."),
                    "--result-filename", result.name]
    return command + list(extra or [])


def guidellm_command(base_url: str, model: str, *, dataset: str = "synthetic_text",
                     prompt_tokens: int = 256, output_tokens: int = 128,
                     rate: float = 4.0, max_seconds: int = 120,
                     output: str | None = None,
                     extra: list[str] | None = None) -> list[str]:
    """GuideLLM v0.7+ command.

    The CLI was rewritten between v0.6 and v0.7: ``benchmark run --target``
    became ``run --backend kind=openai_http,target=...`` and every option is a
    ``kind=`` spec.  GuideLLM discovers the model from ``/v1/models``; the
    ``model`` argument is kept for the caller's metadata and is not passed as
    a flag (it is not accepted by current releases).  Use ``extra`` for
    version-specific options, and pin the GuideLLM version in a measured run.
    """
    if dataset not in ("synthetic_text", "prompt"):
        raise ValueError(
            "GuideLLM's dataset is now a kind= spec; pass synthetic_text or "
            "use --extra with an explicit --data spec")
    prompt = prompt_tokens
    if dataset == "prompt":
        prompt = 512
    command = ["guidellm", "run",
               "--backend", f"kind=openai_http,target={base_url},"
                            "request_format=/v1/chat/completions",
               "--profile", f"kind=constant,rate={rate}",
               "--constraint", f"kind=max_duration,seconds={max_seconds}",
               "--data", f"kind=synthetic_text,prompt_tokens={prompt},"
                         f"output_tokens={output_tokens}"]
    if output:
        command += ["--output", f"kind=json,path={output}"]
    return command + list(extra or [])


def build_command(backend: str, base_url: str, model: str, **kwargs) -> list[str]:
    if backend == "vllm":
        return vllm_command(base_url, model, **kwargs)
    if backend == "guidellm":
        return guidellm_command(base_url, model, **kwargs)
    raise ValueError(f"unknown backend {backend!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="run a serving benchmark")
    parser.add_argument("--backend", choices=("vllm", "guidellm"), default="vllm")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", default=None,
                        help="vllm: random|sharegpt|hf|... ; guidellm: prompt|...")
    parser.add_argument("--num-prompts", type=int, default=200)
    parser.add_argument("--request-rate", type=float, default=4.0)
    parser.add_argument("--max-concurrency", type=int, default=None)
    parser.add_argument("--max-seconds", type=int, default=120)
    parser.add_argument("--prompt-tokens", type=int, default=256,
                        help="GuideLLM synthetic prompt length")
    parser.add_argument("--output-tokens", type=int, default=128,
                        help="GuideLLM synthetic output length")
    parser.add_argument("--output", default=None)
    parser.add_argument("--extra", action="append", default=[],
                        help="extra argument; repeat for multiple")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    kwargs: dict = {"extra": args.extra}
    if args.dataset:
        kwargs["dataset"] = args.dataset
    if args.backend == "vllm":
        kwargs.update(num_prompts=args.num_prompts, request_rate=args.request_rate,
                      max_concurrency=args.max_concurrency, output_json=args.output)
    else:
        kwargs.update(rate=args.request_rate, max_seconds=args.max_seconds,
                      prompt_tokens=args.prompt_tokens,
                      output_tokens=args.output_tokens, output=args.output)
    command = build_command(args.backend, args.base_url, args.model, **kwargs)
    if args.dry_run:
        print(json.dumps({"command": command, "shell": shlex.join(command)}, indent=2))
        return
    raise SystemExit(subprocess.call(command))


if __name__ == "__main__":
    main()
