from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
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
