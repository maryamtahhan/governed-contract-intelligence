"""Semantic cache for the specialist pool (the plan's V5).

Ordering follows the plan: an exact-match lookup is always safe (identical
input, deterministic model); a similarity match is riskier, so it (a) sits
after the exact lookup and (b) never serves or stores sensitive content -
the same PII screen the pipeline uses gates both directions, so a wrongly
inherited answer cannot leak across a sensitive document.

False positives are measurable: every served entry records how it was served
("exact" / "similarity" in the answer JSON), and a validation run can compare
cached answers against freshly computed ones.
"""
from __future__ import annotations

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
    sensitive: bool = False


@dataclass
class CacheStats:
    hits_exact: int = 0
    hits_similarity: int = 0
    misses: int = 0
    stores: int = 0
    sensitive_skips: int = 0
    fp_checks: int = 0
    fp_mismatches: int = 0


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
                if candidate.clause != key[0] or candidate.sensitive:
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

    def check_fp(self, clause: str, context: str, fresh_answer: dict) -> bool:
        """Validation helper: would the cache serve what a fresh compute says?

        Used by the measurement tooling to report the false-positive rate: a
        similarity hit whose answer disagrees with the freshly computed one.
        """
        cached, served_as = self.get(clause, context)
        self.stats.fp_checks += 1
        if cached is None:
            return True
        same = (
            cached.get("found") == fresh_answer.get("found")
            and cached.get("text") == fresh_answer.get("text")
        )
        if not same:
            self.stats.fp_mismatches += 1
        return same

    def snapshot(self) -> dict:
        s = self.stats
        served = s.hits_exact + s.hits_similarity
        return {
            "entries": len(self._entries),
            "hits_exact": s.hits_exact,
            "hits_similarity": s.hits_similarity,
            "misses": s.misses,
            "stores": s.stores,
            "sensitive_skips": s.sensitive_skips,
            "hit_rate": round(served / max(served + s.misses, 1), 3),
            "fp_checks": s.fp_checks,
            "fp_mismatches": s.fp_mismatches,
            "fp_rate": round(s.fp_mismatches / max(s.fp_checks, 1), 3),
        }
