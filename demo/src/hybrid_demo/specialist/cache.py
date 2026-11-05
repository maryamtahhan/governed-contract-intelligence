"""Semantic cache for the specialist pool (the plan's V5).

Ordering follows the plan: an exact-match lookup is always safe (identical
input, deterministic model); a similarity match is riskier, so it (a) sits
after the exact lookup and (b) never serves or stores sensitive content.
Because sensitive content is never stored, there is no sensitive entry to
serve - the exact path cannot return one either.

False positives are measurable: every served entry records how it was served
("exact" / "similarity" in the answer JSON), and a validation run can compare
cached answers against freshly computed ones.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass

from ..classifier import detect_sensitivity
from ..common import cosine, embed, sha256_hex


@dataclass
class CacheEntry:
    fingerprint: str
    clause: str
    vector: list[float]
    answer: dict


@dataclass
class CacheStats:
    hits_exact: int = 0
    hits_similarity: int = 0
    misses: int = 0
    stores: int = 0
    sensitive_skips: int = 0


class SemanticCache:
    def __init__(self, threshold: float = 0.98, max_entries: int = 2048) -> None:
        self.threshold = threshold
        self.max_entries = max_entries
        self._exact: dict[tuple[str, str], CacheEntry] = {}
        self._entries: list[CacheEntry] = []
        self._lock = threading.Lock()
        self.stats = CacheStats()

    def _key(self, clause: str, context: str) -> tuple[str, str]:
        return (clause.strip().lower(), sha256_hex(context))

    def get(self, clause: str, context: str) -> tuple[dict | None, str | None]:
        """Returns (answer, served_as) - answer is None on a miss."""
        key = self._key(clause, context)
        with self._lock:
            entry = self._exact.get(key)
            if entry is not None:
                self.stats.hits_exact += 1
                answer = dict(entry.answer)
                answer["cache"] = "exact"
                return answer, "exact"

            if detect_sensitivity(context):
                self.stats.sensitive_skips += 1
                return None, None

            vector = embed(clause + "\n" + context)
            best, best_score = None, 0.0
            for candidate in self._entries:
                if candidate.clause != key[0]:
                    continue
                score = cosine(vector, candidate.vector)
                if score > best_score:
                    best, best_score = candidate, score
            if best is not None and best_score >= self.threshold:
                self.stats.hits_similarity += 1
                answer = dict(best.answer)
                answer["cache"] = "similarity"
                answer["cache_score"] = round(best_score, 4)
                return answer, "similarity"

            self.stats.misses += 1
            return None, None

    def put(self, clause: str, context: str, answer: dict) -> None:
        if detect_sensitivity(context):
            self.stats.sensitive_skips += 1
            return
        key = self._key(clause, context)
        entry = CacheEntry(
            fingerprint=key[1],
            clause=key[0],
            vector=embed(clause + "\n" + context),
            answer=dict(answer),
        )
        with self._lock:
            self._exact[key] = entry
            self._entries.append(entry)
            while len(self._entries) > self.max_entries:
                evicted = self._entries.pop(0)
                self._exact.pop((evicted.clause, evicted.fingerprint), None)
            self.stats.stores += 1

    def snapshot(self) -> dict:
        s = self.stats
        served = s.hits_exact + s.hits_similarity
        return {
            "backend": "memory",
            "entries": len(self._entries),
            "hits_exact": s.hits_exact,
            "hits_similarity": s.hits_similarity,
            "misses": s.misses,
            "stores": s.stores,
            "sensitive_skips": s.sensitive_skips,
            "hit_rate": round(served / max(served + s.misses, 1), 3),
        }


class RedisSemanticCache:
    """Shared-backend cache: RedisVL for similarity, Redis for exact hits.

    The policy that matters is kept in this class, not delegated: exact lookup
    before similarity, sensitive content never stored or served, and at most
    one similarity candidate per clause.  RedisVL supplies the maintained
    vector-index/eviction machinery; Redis supplies the exact-match key.

    Install with ``uv sync --extra cache`` and point ``--cache-redis-url`` at a
    Redis 7+/Redis Stack instance.  This path is optional because the demo's
    default deployment has no Redis and the in-memory cache is sufficient for
    a single replica.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379", *,
                 threshold: float = 0.98, ttl: int | None = 3600,
                 name: str = "hybrid-specialist-cache") -> None:
        try:
            import redis
            from redisvl.extensions.llmcache import SemanticCache as RedisVLCache
            from redisvl.utils.vectorize import CustomVectorizer
        except ImportError as error:  # pragma: no cover - requires the extra
            raise RuntimeError(
                "redis cache backend requires the 'cache' extra "
                "(uv sync --extra cache) and a running Redis"
            ) from error
        self.threshold = threshold
        self.ttl = ttl
        self.stats = CacheStats()
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        vectorizer = CustomVectorizer(embed=lambda text: embed(text), dtype="float32")
        self._cache = RedisVLCache(name=name, redis_url=redis_url,
                                   vectorizer=vectorizer,
                                   similarity_threshold=threshold, ttl=ttl)

    def _exact_key(self, clause: str, context: str) -> str:
        return "hybrid:exact:" + sha256_hex(clause.strip().lower() + "\n" + context)

    def get(self, clause: str, context: str) -> tuple[dict | None, str | None]:
        exact = self._redis.get(self._exact_key(clause, context))
        if exact:
            self.stats.hits_exact += 1
            answer = json.loads(exact)
            answer["cache"] = "exact"
            return answer, "exact"
        if detect_sensitivity(context):
            self.stats.sensitive_skips += 1
            return None, None
        hits = self._cache.check(prompt=clause + "\n" + context, num_results=5)
        for hit in hits or []:
            metadata = hit.get("metadata") or {}
            if metadata.get("clause") != clause.strip().lower():
                continue
            self.stats.hits_similarity += 1
            answer = json.loads(hit.get("response") or "{}")
            answer["cache"] = "similarity"
            answer["cache_score"] = hit.get("score")
            return answer, "similarity"
        self.stats.misses += 1
        return None, None

    def put(self, clause: str, context: str, answer: dict) -> None:
        if detect_sensitivity(context):
            self.stats.sensitive_skips += 1
            return
        payload = json.dumps(answer, ensure_ascii=False)
        self._redis.set(self._exact_key(clause, context), payload, ex=self.ttl)
        self._cache.store(prompt=clause + "\n" + context, response=payload,
                          metadata={"clause": clause.strip().lower()})
        self.stats.stores += 1

    def snapshot(self) -> dict:
        s = self.stats
        served = s.hits_exact + s.hits_similarity
        return {
            "backend": "redis",
            "hits_exact": s.hits_exact,
            "hits_similarity": s.hits_similarity,
            "misses": s.misses,
            "stores": s.stores,
            "sensitive_skips": s.sensitive_skips,
            "hit_rate": round(served / max(served + s.misses, 1), 3),
        }


def create_cache(backend: str = "memory", *, threshold: float = 0.98,
                 max_entries: int = 2048,
                 redis_url: str = "redis://localhost:6379") -> SemanticCache | RedisSemanticCache:
    """Factory used by the specialist server's ``--cache-backend`` flag."""
    if backend in ("memory", "", "in-process"):
        return SemanticCache(threshold=threshold, max_entries=max_entries)
    if backend == "redis":
        return RedisSemanticCache(redis_url=redis_url, threshold=threshold)
    raise ValueError(f"unknown cache backend {backend!r}")
