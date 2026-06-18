"""
DLP analysis engine: RE2 regex + Aho-Corasick keyword/context matching with
confidence scoring.

Detection vs. action
--------------------
Recall is shape-driven: every RE2 match (and every whole-word denylist keyword
hit) is detected and counted — equivalent to verify.py, independent of context.
Context only *raises a confidence score*; it never drops a match. Each policy
maps the resulting score to an action via its `actions` threshold ladder
(policy.py). "Require context" = a ladder whose no-context band resolves to
"allow". There is a single action mechanism (`actions`); no legacy `action` field.

The specific context word that triggered the boost is recorded on each match
(`Match.context_word`) and aggregated per policy (`Violation.context_words`) for
debugging / audit explainability — the context word is a generic term, never the
matched value.

Two analysis modes
------------------
Plain-text  (analyze):
  RE2 + AC on the full (whitespace-normalized) text. context_word = a context
  word within ±context_range chars (proximity). Match carries start/end.

Tabular      (analyze_tabular):
  RE2 + AC scanned per joined column; row recovered by binary search over
  precomputed cell offsets (O(log rows) per hit). context_word = a context word
  in the column header, the same column, OR the same row. Match carries
  column_name, 1-based row, and sheet.
  A document may ALSO carry free-text body (TabularData.body) — prose paragraphs
  that belong to no table. Those are NOT scanned with column/row context (which
  is unbounded within a column); analyze_tabular runs the plain proximity path
  (analyze) on the joined body and merges the result per policy, so prose in a
  docx/odt behaves exactly like the same content in a .txt/.pdf.

Notes
-----
- Whole-word denylist matching uses Python str.isalnum() boundary checks
  (Unicode-correct for Vietnamese), NOT RE2 \b (ASCII-only).
- RE2 has no look-behind, so PII patterns use \b boundaries.
- Denylist policies may also declare context_words; the same boost applies.
"""

from __future__ import annotations

import bisect
import time
from dataclasses import dataclass, field
from itertools import accumulate
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import ahocorasick
import re2

from policy import ACTION_RANK, Policy, load_policies

if TYPE_CHECKING:
    from extractor import TabularData


def normalize_ws(text: str) -> str:
    """Collapse all whitespace runs (incl. newlines) to single spaces so PII
    wrapped across line breaks during extraction still matches (mirrors
    verify.py). Implemented with str.split()/join (no regex) so it stays cheap
    when called as a per-unit normalizer (one cell / paragraph / line at a
    time). Idempotent, so callers may normalize once and pass the result to
    analyze() with normalize=False."""
    return " ".join(text.split())


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
    # Confidence-scoring fields
    has_context: bool = False
    context_word: str | None = None   # the context word that triggered the boost
    score: float = 0.0
    action: str = "allow"


@dataclass
class Violation:
    policy_id: str
    policy_name: str
    action: str                       # strongest action among this policy's matches
    matches: list[Match]
    context_words: list[str] = field(default_factory=list)  # unique words that boosted any match


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


def _is_word_boundary(text: str, start: int, end: int) -> bool:
    """True if the chars immediately before *start* and at *end* are not
    alphanumeric — Unicode-aware whole-word check (correct for Vietnamese
    diacritics, unlike RE2's ASCII-only \\b)."""
    before_ok = start == 0 or not text[start - 1].isalnum()
    after_ok = end >= len(text) or not text[end].isalnum()
    return before_ok and after_ok


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
                word_map.setdefault(word.lower(), []).append((policy.id, "context"))

            if policy.type == "denylist":
                for kw in policy.keywords:
                    if not kw:
                        continue
                    word_map.setdefault(kw.lower(), []).append((policy.id, "denylist"))

        if not word_map:
            return

        self._automaton = ahocorasick.Automaton()
        for word, entries in word_map.items():
            self._automaton.add_word(word, (word, entries))
        self._automaton.make_automaton()
        self._trie_empty = False

    # ------------------------------------------------------------------
    # Scoring / action
    # ------------------------------------------------------------------

    @staticmethod
    def _score_and_action(policy: Policy, context_word: str | None) -> tuple[float, str]:
        """Return (score, action) for a match of *policy*. Context never gates —
        it only raises the score; the `actions` ladder decides the action."""
        score = policy.score_base + (policy.score_context_boost if context_word else 0.0)
        if score > 1.0:
            score = 1.0
        return score, policy.resolve_action(score)

    # ------------------------------------------------------------------
    # Public API — plain text
    # ------------------------------------------------------------------

    def analyze(self, text: str, channel: str, normalize: bool = True) -> AnalysisResult:
        """Analyze plain text. Matches include start/end character positions.

        When *normalize* is True the text is whitespace-normalized here; callers
        that already normalized per unit (analyze_tabular's body path) pass
        normalize=False to avoid a redundant second pass. Normalization is done
        line-by-line — joining the per-line tokens with a single space — which is
        identical to ``" ".join(text.split())`` (newlines are healed, so PII
        wrapped across a line break still matches) but never materializes every
        word of a multi-MB document at once, keeping the memory peak bounded."""
        t0 = time.perf_counter()
        if normalize:
            text = " ".join(
                norm for line in text.split("\n") if (norm := normalize_ws(line))
            )

        re2_hits, ac_hits, ac_starts = self._scan_text(text, channel)
        violations_map: dict[str, list[Match]] = {}

        # --- RE2 (PII) hits ---
        for start, end, policy in re2_hits:
            cw = (self._context_word_proximity(policy, start, end, ac_hits, ac_starts, text)
                  if policy.context_words else None)
            score, action = self._score_and_action(policy, cw)
            violations_map.setdefault(policy.id, []).append(Match(
                text=text[start:end], start=start, end=end,
                has_context=cw is not None, context_word=cw, score=score, action=action,
            ))

        # --- Denylist keyword hits (single pass over ac_hits) ---
        for hit in ac_hits:
            if hit.tag != "denylist":
                continue
            policy = self._policy_lookup[hit.policy_id]
            if channel not in policy.channels:
                continue
            if not _is_word_boundary(text, hit.start, hit.end):
                continue  # whole-word only
            cw = (self._context_word_proximity(policy, hit.start, hit.end, ac_hits, ac_starts, text)
                  if policy.context_words else None)
            score, action = self._score_and_action(policy, cw)
            violations_map.setdefault(policy.id, []).append(Match(
                text=text[hit.start:hit.end], start=hit.start, end=hit.end,
                has_context=cw is not None, context_word=cw, score=score, action=action,
            ))

        violations = self._build_violations(violations_map, deduplicate=True)
        applied_action = _strongest_action(violations)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return AnalysisResult(applied_action=applied_action, violations=violations, elapsed_ms=elapsed_ms)

    # ------------------------------------------------------------------
    # Public API — tabular
    # ------------------------------------------------------------------

    def analyze_tabular(self, tabular: "TabularData", channel: str) -> AnalysisResult:
        """Analyze column-structured tabular data. Context is satisfied by a
        context word in the column header, the same column, OR the same row; the
        triggering word is recorded on each match."""
        t0 = time.perf_counter()
        cols = tabular.columns

        # Collected raw hits (context resolved in a second pass, since same-row
        # context can come from a column scanned later).
        pii_hits: list[tuple[Policy, int, int, str | None, str]] = []   # policy,col,row,sheet,text
        deny_hits: list[tuple[Policy, int, int, str | None, str]] = []
        col_context: dict[tuple[str, int], str] = {}     # (policy_id, col_idx) -> word (header/column)
        row_context: dict[str, dict[int, str]] = {}      # policy_id -> {row -> word} (any column)

        for col_idx, col in enumerate(cols):
            # Normalize each cell (collapse internal whitespace incl. newlines) so
            # PII wrapped inside a cell still matches. Per-cell normalization keeps
            # the lengths used for row-offset recovery exact and never builds one
            # giant token list for the whole column.
            values = [normalize_ws(v) for v in col.values]
            joined = "\n".join(values)
            joined_lower = joined.lower()
            # cell-start offsets: offsets[i] = start of cell i in `joined`
            offsets = [0, *accumulate(len(v) + 1 for v in values)]
            hdr_lower = (col.header or "").lower()

            # header context (substring, case-insensitive) — store the policy's word
            if hdr_lower:
                for policy in self._policies:
                    if (policy.id, col_idx) in col_context:
                        continue
                    for cw in policy.context_words:
                        if cw and cw.lower() in hdr_lower:
                            col_context[(policy.id, col_idx)] = cw
                            break

            # RE2 (PII) — one finditer per policy over the joined column
            for cp in self._compiled:
                if channel not in cp.policy.channels:
                    continue
                for m in cp.pattern.finditer(joined):
                    row = bisect.bisect_right(offsets, m.start()) - 1
                    pii_hits.append((cp.policy, col_idx, row, col.sheet, joined[m.start():m.end()]))

            # AC — context + denylist over the joined column (one pass)
            if not self._trie_empty and self._automaton is not None:
                for end_idx, (word, entries) in self._automaton.iter(joined_lower):
                    start_idx = end_idx - len(word) + 1
                    end_exc = end_idx + 1
                    row = bisect.bisect_right(offsets, start_idx) - 1
                    for policy_id, tag in entries:
                        if tag == "context":
                            actual = joined[start_idx:end_exc]
                            col_context.setdefault((policy_id, col_idx), actual)
                            row_context.setdefault(policy_id, {}).setdefault(row, actual)
                        else:  # denylist
                            policy = self._policy_lookup.get(policy_id)
                            if policy is None or channel not in policy.channels:
                                continue
                            if not _is_word_boundary(joined, start_idx, end_exc):
                                continue
                            deny_hits.append((policy, col_idx, row, col.sheet,
                                              joined[start_idx:end_exc]))

        def _ctx_word(policy: Policy, col_idx: int, row: int) -> str | None:
            if not policy.context_words:
                return None
            w = col_context.get((policy.id, col_idx))
            if w:
                return w
            rc = row_context.get(policy.id)
            return rc.get(row) if rc else None

        violations_map: dict[str, list[Match]] = {}
        for policy, col_idx, row, sheet, text in (*pii_hits, *deny_hits):
            cw = _ctx_word(policy, col_idx, row)
            score, action = self._score_and_action(policy, cw)
            violations_map.setdefault(policy.id, []).append(Match(
                text=text,
                column_name=cols[col_idx].header or None,
                row=row + 1,
                sheet=sheet,
                has_context=cw is not None, context_word=cw, score=score, action=action,
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
            v.action = _strongest_match_action(deduped)
            v.context_words = _collect_context_words(deduped)

        # --- Free-text body: bounded character proximity, NOT column/row context ---
        # Body paragraphs (docx/odt prose, etc.) have no table structure, so the
        # column/row context model above would credit any context word anywhere in
        # the body to every body match (unbounded). Run the plain proximity engine
        # on the joined body instead — identical semantics to the same document's
        # plain-text/PDF twin — and merge the results per policy. The body is
        # scanned exactly once here; table columns were scanned above; the two
        # cover disjoint content (no double scan).
        if tabular.body:
            # Normalize per paragraph (small units → bounded memory; a 93 MB
            # body no longer triggers a whole-string normalize), then run the
            # plain proximity engine with normalize=False. Paragraphs are joined
            # by a single "\n", so cross-paragraph character proximity is
            # unchanged (the boundary is still one character).
            body_text = "\n".join(normalize_ws(p) for p in tabular.body)
            body_violations = self.analyze(body_text, channel, normalize=False).violations
            violations = self._merge_violations(violations, body_violations)

        applied_action = _strongest_action(violations)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return AnalysisResult(applied_action=applied_action, violations=violations, elapsed_ms=elapsed_ms)

    def _merge_violations(
        self, a: list[Violation], b: list[Violation]
    ) -> list[Violation]:
        """Combine two per-policy violation lists into one. Tabular matches
        (column/row) and plain body matches (start/end) reference disjoint
        content and are each already deduplicated within their own pass, so the
        merge only needs to union matches by policy id and recompute the
        per-policy action / context words. Policy order is preserved (tables
        first, then body-only policies)."""
        by_id: dict[str, list[Match]] = {}
        order: list[str] = []
        for v in (*a, *b):
            if v.policy_id not in by_id:
                order.append(v.policy_id)
            by_id.setdefault(v.policy_id, []).extend(v.matches)
        merged: list[Violation] = []
        for pid in order:
            matches = by_id[pid]
            merged.append(Violation(
                policy_id=pid,
                policy_name=self._policy_lookup[pid].name,
                action=_strongest_match_action(matches),
                matches=matches,
                context_words=_collect_context_words(matches),
            ))
        return merged

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _scan_text(
        self, text: str, channel: str
    ) -> tuple[list[tuple[int, int, Policy]], list[_ACHit], list[int]]:
        """Run RE2 and AC scanning. Returns raw hits (no context resolution).
        ac_hits keeps every automaton hit (context + denylist) for proximity;
        whole-word filtering for denylist is applied by the caller."""
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
                action=_strongest_match_action(matches),
                matches=matches,
                context_words=_collect_context_words(matches),
            ))
        return violations

    @staticmethod
    def _context_word_proximity(
        policy: Policy,
        hit_start: int,
        hit_end: int,
        ac_hits: list[_ACHit],
        ac_starts: list[int],
        text: str,
    ) -> str | None:
        """Return the context word for *policy* within the character window, or None."""
        window_start = max(0, hit_start - policy.context_range)
        window_end = hit_end + policy.context_range

        lo = bisect.bisect_left(ac_starts, window_start)
        for i in range(lo, len(ac_hits)):
            h = ac_hits[i]
            if h.start > window_end:
                break
            if h.tag == "context" and h.policy_id == policy.id and h.end <= window_end:
                return text[h.start:h.end]
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strongest_action(violations: list[Violation]) -> str:
    action = "allow"
    for v in violations:
        if ACTION_RANK.get(v.action, 0) > ACTION_RANK.get(action, 0):
            action = v.action
    return action


def _strongest_match_action(matches: list[Match]) -> str:
    action = "allow"
    for m in matches:
        if ACTION_RANK.get(m.action, 0) > ACTION_RANK.get(action, 0):
            action = m.action
    return action


def _collect_context_words(matches: list[Match]) -> list[str]:
    """Sorted-unique context words that boosted any match of a policy."""
    return sorted({m.context_word for m in matches if m.context_word})


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
