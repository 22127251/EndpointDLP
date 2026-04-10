"""
Pattern-only analyzer service (no NLP engine).

Presidio's PatternRecognizer.analyze() is called directly with nlp_artifacts=None.
The NLP engine (Stanza) and AnalyzerEngine are not used — this eliminates the
~900ms per-chunk NLP overhead while keeping Presidio's regex/denylist machinery.

Context word filtering replaces Presidio's NLP-based context boost: a match is
included only if any configured context word appears within ±CONTEXT_WINDOW chars
of the matched span.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from policy import Action, Policy, load_policies, strongest_action
from recognizer_factory import RecognizerPair, build_recognizer_pairs

log = logging.getLogger(__name__)

CONTEXT_WINDOW = 100  # characters to search on each side of a match


# ---------------------------------------------------------------------------
# Context helper
# ---------------------------------------------------------------------------

def _has_context(text: str, start: int, end: int, context_words: list[str]) -> bool:
    """
    Return True if at least one context word appears within CONTEXT_WINDOW chars
    of the matched span, or if the policy defines no context words.
    """
    if not context_words:
        return True
    snippet = text[max(0, start - CONTEXT_WINDOW) : end + CONTEXT_WINDOW].lower()
    return any(w.lower() in snippet for w in context_words)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class MatchDetail:
    start: int
    end: int
    score: float
    matched_text: str


@dataclass
class Violation:
    policy_id: str
    policy_name: str
    action: Action
    matches: list[MatchDetail] = field(default_factory=list)


@dataclass
class AnalysisResult:
    chunk_id: str
    applied_action: Action
    violations: list[Violation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "applied_action": self.applied_action,
            "violations": [
                {
                    "policy_id": v.policy_id,
                    "policy_name": v.policy_name,
                    "action": v.action,
                    "matches": [
                        {
                            "start": m.start,
                            "end": m.end,
                            "score": round(m.score, 4),
                            "matched_text": m.matched_text,
                        }
                        for m in v.matches
                    ],
                }
                for v in self.violations
            ],
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class AnalyzerService:
    def __init__(self, policies: list[Policy]):
        self._policy_map: dict[str, Policy] = {p.id: p for p in policies}
        self._all_pairs: list[RecognizerPair] = build_recognizer_pairs(policies)
        # Timing state — overwritten each request (safe: connections are serialized)
        self._recognizer_timings: dict[str, tuple[int, float]] = {}  # name → (hits, ms)
        log.info("AnalyzerService ready (%d recognizers).", len(self._all_pairs))

    def analyze(self, request: dict[str, Any]) -> AnalysisResult:
        chunk_id: str = request.get("chunk_id", "")
        text: str = request.get("text", "")
        metadata: dict = request.get("metadata", {})
        channel: str = metadata.get("channel", "")

        # Filter to recognizers whose policy applies to this channel
        active_pairs = [
            (rec, pol) for rec, pol in self._all_pairs
            if not pol.channels or channel in pol.channels
        ]

        if not active_pairs:
            return AnalysisResult(chunk_id=chunk_id, applied_action="allow_no_log")

        self._recognizer_timings.clear()
        groups: dict[str, list] = {}

        for recognizer, policy in active_pairs:
            t0 = time.perf_counter()
            raw = recognizer.analyze(
                text=text,
                entities=[policy.id],
                nlp_artifacts=None,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000

            context_words = policy.context if hasattr(policy, "context") else []
            hits = [
                r for r in raw
                if _has_context(text, r.start, r.end, context_words)
            ]
            self._recognizer_timings[recognizer.name] = (len(hits), elapsed_ms)
            for r in hits:
                groups.setdefault(r.entity_type, []).append(r)

        if log.isEnabledFor(logging.DEBUG):
            total_rec_ms = sum(ms for _, ms in self._recognizer_timings.values())
            log.debug("chunk=%s", chunk_id[:8])
            log.debug("  recognizers: %7.1fms  (total)", total_rec_ms)
            for name, (hits, ms) in sorted(
                self._recognizer_timings.items(), key=lambda x: -x[1][1]
            ):
                hit_label = f"{hits} hit{'s' if hits != 1 else ''}"
                log.debug("    %-40s %6s  %5.1fms", name, hit_label, ms)

        violations: list[Violation] = []
        for policy_id, results in groups.items():
            policy = self._policy_map.get(policy_id)
            if policy is None:
                continue
            matches = [
                MatchDetail(
                    start=r.start,
                    end=r.end,
                    score=r.score,
                    matched_text=text[r.start:r.end],
                )
                for r in results
            ]
            violations.append(Violation(
                policy_id=policy_id,
                policy_name=policy.name,
                action=policy.action,
                matches=matches,
            ))

        applied = strongest_action([v.action for v in violations])
        return AnalysisResult(
            chunk_id=chunk_id,
            applied_action=applied,
            violations=violations,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_service(config_path: str, policies_path: str) -> AnalyzerService:
    import os
    import yaml
    if not os.path.exists(config_path):
        pass  # config only needed for pipe_name in pipe_server.py
    policies = load_policies(policies_path)
    return AnalyzerService(policies)
