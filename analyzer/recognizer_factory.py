"""
Builds Presidio PatternRecognizers from Policy objects.

Returns list of (PatternRecognizer, Policy) pairs so the caller retains access
to the policy's context words for windowed context checking (Presidio's built-in
context enhancement requires nlp_artifacts which we no longer provide).

One recognizer per policy — no language duplication needed since we bypass
AnalyzerEngine's language dispatch and call recognizers directly.
"""

from __future__ import annotations

from presidio_analyzer import Pattern, PatternRecognizer

from policy import DenylistPolicy, Policy, RegexPolicy

RecognizerPair = tuple[PatternRecognizer, Policy]


def build_recognizer_pairs(policies: list[Policy]) -> list[RecognizerPair]:
    """Return (PatternRecognizer, Policy) pairs for all policies."""
    pairs: list[RecognizerPair] = []
    for policy in policies:
        if isinstance(policy, RegexPolicy):
            pairs.append(_regex_pair(policy))
        elif isinstance(policy, DenylistPolicy):
            pairs.append(_denylist_pair(policy))
    return pairs


def _regex_pair(policy: RegexPolicy) -> RecognizerPair:
    patterns = [
        Pattern(name=p.name, regex=p.regex, score=p.score)
        for p in policy.patterns
    ]
    recognizer = PatternRecognizer(
        supported_entity=policy.id,
        name=policy.id,
        patterns=patterns,
        # context is intentionally NOT passed here — context word checking is
        # done by the caller using _has_context() since nlp_artifacts=None means
        # Presidio's built-in context boost would be silently skipped anyway.
    )
    return recognizer, policy


def _denylist_pair(policy: DenylistPolicy) -> RecognizerPair:
    recognizer = PatternRecognizer(
        supported_entity=policy.id,
        name=policy.id,
        deny_list=policy.deny_list,
    )
    return recognizer, policy
