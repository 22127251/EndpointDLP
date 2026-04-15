"""
DLP analysis engine: RE2 regex + Aho-Corasick denylist/context matching.

Flow
----
Init:
  1. Load policies from YAML.
  2. Compile each regex policy's patterns into one RE2 pattern (union of all patterns).
  3. Build a single Aho-Corasick automaton over:
       - context_words  (tagged as "context")
       - denylist keywords (tagged as "denylist")
     Each automaton value is a list of (policy_id, tag) pairs so multiple
     policies can share the same word.

Analyze(text, channel):
  1. RE2 phase  - run compiled patterns that include this channel.
  2. AC phase   - run the automaton once over the full text (if non-empty).
  3. For each RE2 hit:
       - policy has context_words → binary-search AC results for a matching
         context hit within [hit.start - range, hit.end + range].
       - policy has no context_words → unconditional true match.
  4. For each denylist policy:
       - collect AC hits tagged "denylist" for that policy.
       - policy has context_words → binary-search for a context hit in window.
       - policy has no context_words → all keyword hits are true matches.
  5. Determine applied_action = strongest action across all violations.
     All violations (block AND allow_log) are always returned.
"""

from __future__ import annotations

import bisect
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import ahocorasick
import re2

from policy import ACTION_RANK, Policy, load_policies


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Match:
    start: int
    end: int
    text: str


@dataclass
class Violation:
    policy_id: str
    policy_name: str
    action: str
    matches: list[Match]


@dataclass
class AnalysisResult:
    applied_action: str
    violations: list[Violation]
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _ACHit(NamedTuple):
    """A single Aho-Corasick match position."""
    start: int   # inclusive
    end: int     # exclusive
    policy_id: str
    tag: str     # "context" | "denylist"


class _CompiledPolicy(NamedTuple):
    pattern: re2.Pattern
    policy: Policy


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class DLPEngine:
    def __init__(self, policy_file: str | Path) -> None:
        self._policies = load_policies(policy_file)
        self._compiled: list[_CompiledPolicy] = []
        self._automaton: ahocorasick.Automaton | None = None
        self._trie_empty = True

        self._build_regex()
        self._build_automaton()

    # ------------------------------------------------------------------
    # Init helpers
    # ------------------------------------------------------------------

    def _build_regex(self) -> None:
        for policy in self._policies:
            if policy.type != "regex" or not policy.patterns:
                continue
            # Join all patterns as alternatives; wrap each in a non-capturing group.
            combined = "|".join(f"(?:{p})" for p in policy.patterns)
            compiled = re2.compile(combined)
            self._compiled.append(_CompiledPolicy(compiled, policy))

    def _build_automaton(self) -> None:
        # Collect all strings that need to go into the trie.
        # word → list of (policy_id, tag)
        word_map: dict[str, list[tuple[str, str]]] = {}

        for policy in self._policies:
            for word in policy.context_words:
                if not word:
                    continue
                word_lower = word.lower()
                word_map.setdefault(word_lower, []).append((policy.id, "context"))

            if policy.type == "denylist":
                for kw in policy.keywords:
                    if not kw:
                        continue
                    kw_lower = kw.lower()
                    word_map.setdefault(kw_lower, []).append((policy.id, "denylist"))

        if not word_map:
            return

        self._automaton = ahocorasick.Automaton()
        for word, entries in word_map.items():
            self._automaton.add_word(word, (word, entries))
        self._automaton.make_automaton()
        self._trie_empty = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, text: str, channel: str) -> AnalysisResult:
        t0 = time.perf_counter()

        text_lower = text.lower()

        # --- RE2 phase ---
        re2_hits: list[tuple[int, int, Policy]] = []
        for cp in self._compiled:
            if channel not in cp.policy.channels:
                continue
            for m in cp.pattern.finditer(text):
                re2_hits.append((m.start(), m.end(), cp.policy))

        # --- Aho-Corasick phase ---
        ac_hits: list[_ACHit] = []
        if not self._trie_empty and self._automaton is not None:
            for end_idx, (word, entries) in self._automaton.iter(text_lower):
                start_idx = end_idx - len(word) + 1
                end_exc = end_idx + 1  # convert to exclusive
                for policy_id, tag in entries:
                    ac_hits.append(_ACHit(start_idx, end_exc, policy_id, tag))
            # Sort by start position for binary search.
            ac_hits.sort(key=lambda h: h.start)

        ac_starts = [h.start for h in ac_hits]  # parallel list for bisect

        # --- Resolve RE2 hits ---
        # policy_id → list of Match
        violations_map: dict[str, list[Match]] = {}

        for start, end, policy in re2_hits:
            if channel not in policy.channels:
                continue
            matched_text = text[start:end]

            if not policy.context_words:
                # No context required – unconditional match.
                violations_map.setdefault(policy.id, []).append(
                    Match(start, end, matched_text)
                )
            else:
                if self._has_context(policy, start, end, ac_hits, ac_starts):
                    violations_map.setdefault(policy.id, []).append(
                        Match(start, end, matched_text)
                    )

        # --- Resolve denylist hits ---
        for policy in self._policies:
            if policy.type != "denylist":
                continue
            if channel not in policy.channels:
                continue

            denylist_hits = [
                h for h in ac_hits
                if h.tag == "denylist" and h.policy_id == policy.id
            ]

            for hit in denylist_hits:
                matched_text = text[hit.start:hit.end]
                if not policy.context_words:
                    violations_map.setdefault(policy.id, []).append(
                        Match(hit.start, hit.end, matched_text)
                    )
                else:
                    if self._has_context(policy, hit.start, hit.end, ac_hits, ac_starts):
                        violations_map.setdefault(policy.id, []).append(
                            Match(hit.start, hit.end, matched_text)
                        )

        # --- Deduplicate overlapping matches per policy ---
        policy_lookup = {p.id: p for p in self._policies}
        violations: list[Violation] = []
        for policy_id, matches in violations_map.items():
            deduped = _deduplicate(matches)
            policy = policy_lookup[policy_id]
            violations.append(Violation(
                policy_id=policy_id,
                policy_name=policy.name,
                action=policy.action,
                matches=deduped,
            ))

        # --- Applied action ---
        applied_action = "allow"
        for v in violations:
            if ACTION_RANK.get(v.action, 0) > ACTION_RANK.get(applied_action, 0):
                applied_action = v.action

        elapsed_ms = (time.perf_counter() - t0) * 1000
        return AnalysisResult(
            applied_action=applied_action,
            violations=violations,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _has_context(
        policy: Policy,
        hit_start: int,
        hit_end: int,
        ac_hits: list[_ACHit],
        ac_starts: list[int],
    ) -> bool:
        """Return True if any context word for *policy* falls within the window."""
        window_start = max(0, hit_start - policy.context_range)
        window_end = hit_end + policy.context_range

        # Binary search to the first hit that could be in the window.
        lo = bisect.bisect_left(ac_starts, window_start)
        for i in range(lo, len(ac_hits)):
            h = ac_hits[i]
            if h.start > window_end:
                break
            if h.tag == "context" and h.policy_id == policy.id and h.end <= window_end:
                return True
        return False


def _deduplicate(matches: list[Match]) -> list[Match]:
    """Remove matches that are fully contained within another match."""
    if not matches:
        return matches
    # Sort by start, then by length descending.
    sorted_m = sorted(matches, key=lambda m: (m.start, -(m.end - m.start)))
    result: list[Match] = []
    last_end = -1
    for m in sorted_m:
        if m.start >= last_end:
            result.append(m) 
            last_end = m.end
    return result
