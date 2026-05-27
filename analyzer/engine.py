"""
DLP analysis engine: RE2 regex + Aho-Corasick denylist/context matching.

Two analysis modes
------------------
Plain-text  (analyze):
  RE2 + AC on the full text. Context words confirmed by character proximity
  (AC hit within ±context_range chars of the regex/denylist match).
  Match carries start/end character positions.

Tabular      (analyze_tabular):
  RE2 scanned per-cell (avoids cross-cell false positives).
  AC scanned on the joined column text for denylist keywords.
  Context words confirmed by column-header matching (not character proximity).
  Match carries column_name, 1-based row, and sheet name.
"""

from __future__ import annotations

import bisect
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import ahocorasick
import re2

from policy import ACTION_RANK, Policy, load_policies

if TYPE_CHECKING:
    from extractor import TabularData


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Match:
    text: str
    # Plain-text fields (None for tabular matches)
    start: int | None = None
    end: int | None = None
    # Tabular fields (None for plain-text matches)
    column_name: str | None = None
    row: int | None = None      # 1-based data row (header row not counted)
    sheet: str | None = None    # None for single-sheet formats (CSV)


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
        self._policy_lookup: dict[str, Policy] = {p.id: p for p in self._policies}

        self._build_regex()
        self._build_automaton()

    # ------------------------------------------------------------------
    # Init helpers
    # ------------------------------------------------------------------

    def _build_regex(self) -> None:
        for policy in self._policies:
            if policy.type != "regex" or not policy.patterns:
                continue
            combined = "|".join(f"(?:{p})" for p in policy.patterns)
            compiled = re2.compile(combined)
            self._compiled.append(_CompiledPolicy(compiled, policy))

    def _build_automaton(self) -> None:
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
    # Public API — plain text
    # ------------------------------------------------------------------

    def analyze(self, text: str, channel: str) -> AnalysisResult:
        """Analyze plain text. Matches include start/end character positions."""
        t0 = time.perf_counter()

        re2_hits, ac_hits, ac_starts = self._scan_text(text, channel)

        violations_map: dict[str, list[Match]] = {}

        # --- Resolve RE2 hits ---
        for start, end, policy in re2_hits:
            matched_text = text[start:end]
            if not policy.context_words:
                violations_map.setdefault(policy.id, []).append(
                    Match(text=matched_text, start=start, end=end)
                )
            elif self._has_context_proximity(policy, start, end, ac_hits, ac_starts):
                violations_map.setdefault(policy.id, []).append(
                    Match(text=matched_text, start=start, end=end)
                )

        # --- Resolve denylist hits ---
        for policy in self._policies:
            if policy.type != "denylist" or channel not in policy.channels:
                continue
            for hit in ac_hits:
                if hit.tag != "denylist" or hit.policy_id != policy.id:
                    continue
                matched_text = text[hit.start:hit.end]
                if not policy.context_words:
                    violations_map.setdefault(policy.id, []).append(
                        Match(text=matched_text, start=hit.start, end=hit.end)
                    )
                elif self._has_context_proximity(policy, hit.start, hit.end, ac_hits, ac_starts):
                    violations_map.setdefault(policy.id, []).append(
                        Match(text=matched_text, start=hit.start, end=hit.end)
                    )

        violations = self._build_violations(violations_map, deduplicate=True)
        applied_action = _strongest_action(violations)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return AnalysisResult(applied_action=applied_action, violations=violations, elapsed_ms=elapsed_ms)

    # ------------------------------------------------------------------
    # Public API — tabular
    # ------------------------------------------------------------------

    def analyze_tabular(self, tabular: TabularData, channel: str) -> AnalysisResult:
        """
        Analyze column-structured tabular data.
        Context is determined by column-header matching, not character proximity.
        Matches include column_name, 1-based row, and sheet name.
        """
        t0 = time.perf_counter()
        violations_map: dict[str, list[Match]] = {}

        for col in tabular.columns:
            col_text = "\n".join(col.values)
            col_text_lower = col_text.lower()
            is_body_col = not col.header  # sentinel: body paragraph / plain-text column

            # --- RE2 phase: scan per-cell to avoid cross-cell false positives ---
            for cp in self._compiled:
                if channel not in cp.policy.channels:
                    continue
                policy = cp.policy

                if is_body_col:
                    # Body text column: check context word inline within each cell
                    for row_idx, cell_value in enumerate(col.values):
                        if not cell_value:
                            continue
                        if policy.context_words:
                            cell_lower = cell_value.lower()
                            if not any(cw.lower() in cell_lower for cw in policy.context_words):
                                continue
                        for m in cp.pattern.finditer(cell_value):
                            violations_map.setdefault(policy.id, []).append(Match(
                                text=cell_value[m.start():m.end()],
                                column_name=col.header,
                                row=row_idx + 1,
                                sheet=col.sheet,
                            ))
                else:
                    # Named column: header-based context matching
                    if policy.context_words and not self._header_matches_context(col.header, policy):
                        continue
                    for row_idx, cell_value in enumerate(col.values):
                        if not cell_value:
                            continue
                        for m in cp.pattern.finditer(cell_value):
                            violations_map.setdefault(policy.id, []).append(Match(
                                text=cell_value[m.start():m.end()],
                                column_name=col.header,
                                row=row_idx + 1,
                                sheet=col.sheet,
                            ))

            # --- AC phase: denylist keywords on joined column text ---
            if not self._trie_empty and self._automaton is not None:
                for end_idx, (word, entries) in self._automaton.iter(col_text_lower):
                    start_idx = end_idx - len(word) + 1
                    end_exc = end_idx + 1
                    for policy_id, tag in entries:
                        if tag != "denylist":
                            continue
                        policy = self._policy_lookup.get(policy_id)
                        if policy is None or channel not in policy.channels:
                            continue
                        if policy.context_words:
                            if is_body_col:
                                row_idx = col_text[:start_idx].count("\n")
                                cell_val = col.values[row_idx] if row_idx < len(col.values) else ""
                                if not any(cw.lower() in cell_val.lower() for cw in policy.context_words):
                                    continue
                            elif not self._header_matches_context(col.header, policy):
                                continue
                        row = col_text[:start_idx].count("\n") + 1
                        matched_text = col_text[start_idx:end_exc]
                        violations_map.setdefault(policy_id, []).append(Match(
                            text=matched_text,
                            column_name=col.header,
                            row=row,
                            sheet=col.sheet,
                        ))

        violations = self._build_violations(violations_map, deduplicate=False)
        # Deduplicate tabular matches by (column_name, row, sheet, text) identity
        for v in violations:
            seen: set[tuple] = set()
            deduped: list[Match] = []
            for m in v.matches:
                key = (m.column_name, m.row, m.sheet, m.text)
                if key not in seen:
                    seen.add(key)
                    deduped.append(m)
            v.matches = deduped

        applied_action = _strongest_action(violations)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return AnalysisResult(applied_action=applied_action, violations=violations, elapsed_ms=elapsed_ms)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _scan_text(
        self, text: str, channel: str
    ) -> tuple[list[tuple[int, int, Policy]], list[_ACHit], list[int]]:
        """Run RE2 and AC scanning. Returns raw hits (no context resolution)."""
        text_lower = text.lower()

        re2_hits: list[tuple[int, int, Policy]] = []
        for cp in self._compiled:
            if channel not in cp.policy.channels:
                continue
            for m in cp.pattern.finditer(text):
                re2_hits.append((m.start(), m.end(), cp.policy))

        ac_hits: list[_ACHit] = []
        if not self._trie_empty and self._automaton is not None:
            for end_idx, (word, entries) in self._automaton.iter(text_lower):
                start_idx = end_idx - len(word) + 1
                end_exc = end_idx + 1
                for policy_id, tag in entries:
                    ac_hits.append(_ACHit(start_idx, end_exc, policy_id, tag))
            ac_hits.sort(key=lambda h: h.start)

        ac_starts = [h.start for h in ac_hits]
        return re2_hits, ac_hits, ac_starts

    def _build_violations(
        self, violations_map: dict[str, list[Match]], deduplicate: bool
    ) -> list[Violation]:
        violations: list[Violation] = []
        for policy_id, matches in violations_map.items():
            policy = self._policy_lookup[policy_id]
            if deduplicate:
                matches = _deduplicate_plain(matches)
            violations.append(Violation(
                policy_id=policy_id,
                policy_name=policy.name,
                action=policy.action,
                matches=matches,
            ))
        return violations

    @staticmethod
    def _has_context_proximity(
        policy: Policy,
        hit_start: int,
        hit_end: int,
        ac_hits: list[_ACHit],
        ac_starts: list[int],
    ) -> bool:
        """Return True if a context word for *policy* falls within the character window."""
        window_start = max(0, hit_start - policy.context_range)
        window_end = hit_end + policy.context_range

        lo = bisect.bisect_left(ac_starts, window_start)
        for i in range(lo, len(ac_hits)):
            h = ac_hits[i]
            if h.start > window_end:
                break
            if h.tag == "context" and h.policy_id == policy.id and h.end <= window_end:
                return True
        return False

    @staticmethod
    def _header_matches_context(header: str, policy: Policy) -> bool:
        """Return True if the column header matches any context word (substring, case-insensitive)."""
        h = header.lower()
        if not h:
            return False
        return any(cw.lower() in h or h in cw.lower() for cw in policy.context_words)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strongest_action(violations: list[Violation]) -> str:
    action = "allow"
    for v in violations:
        if ACTION_RANK.get(v.action, 0) > ACTION_RANK.get(action, 0):
            action = v.action
    return action


def _deduplicate_plain(matches: list[Match]) -> list[Match]:
    """Remove plain-text matches fully contained within another (by character span)."""
    if not matches:
        return matches
    sorted_m = sorted(matches, key=lambda m: (m.start, -(m.end - m.start)))  # type: ignore[operator]
    result: list[Match] = []
    last_end = -1
    for m in sorted_m:
        if m.start >= last_end:  # type: ignore[operator]
            result.append(m)
            last_end = m.end  # type: ignore[assignment]
    return result
