from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

NGRAM = 3
EMBED_DIM = 256


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8", "replace")).hexdigest()


_WS = re.compile(r"\s+")


def normalize_text(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip().lower()


def est_tokens(text: str | None) -> int:
    return max(1, (len(text or "") + 3) // 4)


def embed(text: str) -> list[float]:
    t = normalize_text(text)
    vec = [0.0] * EMBED_DIM
    for i in range(len(t) - NGRAM + 1):
        h = int.from_bytes(hashlib.sha1(t[i : i + NGRAM].encode()).digest()[:8], "big")
        vec[h % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def request_fingerprint(body: dict[str, Any]) -> str:
    key = {
        "model": body.get("model"),
        "messages": body.get("messages"),
        "temperature": body.get("temperature"),
        "max_tokens": body.get("max_tokens"),
        "tools": body.get("tools"),
    }
    return sha256_hex(json.dumps(key, sort_keys=True, ensure_ascii=False))


def prompt_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for m in body.get("messages") or []:
        if isinstance(m, dict):
            content = m.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        parts.append(c["text"])
    if body.get("tools"):
        parts.append(json.dumps(body["tools"], sort_keys=True))
    return "\n".join(parts)


def parse_url_host_port(url: str) -> tuple[str, int]:
    m = re.match(r"^https?://([^/:]+)(?::(\d+))?", url)
    if not m:
        raise ValueError(f"bad replica url: {url}")
    host = m.group(1)
    port = int(m.group(2) or (443 if url.startswith("https") else 80))
    return host, port


def _git_sha() -> str | None:
    """The commit a run was produced from, when this checkout knows it.

    A deployed image has no .git; HYBRID_GIT_SHA can be baked in at build
    time, and None is the honest answer when neither exists.
    """
    override = os.environ.get("HYBRID_GIT_SHA")
    if override:
        return override
    root = Path(__file__).resolve().parents[3]
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and sha else None


def _git_dirty() -> bool | None:
    """Whether the tree that produced the run matched its HEAD.

    A sha with uncommitted changes is a base, not the code; recording that
    stops a later comparison from crediting the wrong commit. None when this
    is not a git checkout at all (a deployed image).
    """
    root = Path(__file__).resolve().parents[3]
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return bool(done.stdout.strip())


def parse_pools(raw: str | None) -> dict:
    """Pool provenance passed as JSON or an ``@path``.

    The runner cannot discover which checkpoint or hardware sat behind a
    route; only the operator knows, at deploy time. Recording it with the
    run is the difference between a replay and a screenshot.
    """
    if not raw:
        return {}
    text = Path(raw[1:]).read_text() if raw.startswith("@") else raw
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("--pools must be a JSON object")
    return value


def run_provenance(environment: str | None = None, cluster: str | None = None,
                   pools: dict | None = None, **extra: Any) -> dict[str, Any]:
    """Where a run happened, recorded with it.

    ``environment``/``cluster`` fall back to HYBRID_ENVIRONMENT and
    HYBRID_CLUSTER, so a cluster script can set them once for every arm.
    """
    return {
        "environment": (environment or os.environ.get("HYBRID_ENVIRONMENT")
                        or None),
        "cluster": cluster or os.environ.get("HYBRID_CLUSTER") or None,
        "git_sha": _git_sha(),
        "git_dirty": _git_dirty(),
        "pools": pools or {},
        **extra,
    }
