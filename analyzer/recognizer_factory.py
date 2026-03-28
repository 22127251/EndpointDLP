"""
Builds Presidio recognizers from Policy objects.

Rules:
  - RegexPolicy / DenylistPolicy  → PatternRecognizer, registered for both 'en' and 'vi'
    (regex/keyword rules are language-independent; Presidio filters by language at
     analysis time, so we must register for each language we analyze).
  - NerEntityPolicy               → NerPolicyRecognizer (custom EntityRecognizer),
    registered for the policy's declared language, or both if language is None.
"""

from __future__ import annotations

from typing import List

from presidio_analyzer import EntityRecognizer, Pattern, PatternRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

from policy import DenylistPolicy, NerEntityPolicy, Policy, RegexPolicy

_ALL_LANGS = ["en", "vi"]


def build_recognizers(policies: list[Policy]) -> list[EntityRecognizer]:
    """Return a flat list of Presidio recognizers covering all policies."""
    recognizers: list[EntityRecognizer] = []
    for policy in policies:
        if isinstance(policy, RegexPolicy):
            recognizers.extend(_regex_recognizers(policy))
        elif isinstance(policy, DenylistPolicy):
            recognizers.extend(_denylist_recognizers(policy))
        elif isinstance(policy, NerEntityPolicy):
            recognizers.extend(_ner_recognizers(policy))
    return recognizers


# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------

def _regex_recognizers(policy: RegexPolicy) -> list[PatternRecognizer]:
    patterns = [
        Pattern(name=p.name, regex=p.regex, score=p.score)
        for p in policy.patterns
    ]
    return [
        PatternRecognizer(
            supported_entity=policy.id,
            name=f"{policy.id}__{lang}",
            patterns=patterns,
            context=policy.context or None,
            supported_language=lang,
        )
        for lang in _ALL_LANGS
    ]


# ---------------------------------------------------------------------------
# Denylist
# ---------------------------------------------------------------------------

def _denylist_recognizers(policy: DenylistPolicy) -> list[PatternRecognizer]:
    return [
        PatternRecognizer(
            supported_entity=policy.id,
            name=f"{policy.id}__{lang}",
            deny_list=policy.deny_list,
            context=policy.context or None,
            supported_language=lang,
        )
        for lang in _ALL_LANGS
    ]


# ---------------------------------------------------------------------------
# NER entity
# ---------------------------------------------------------------------------

class NerPolicyRecognizer(EntityRecognizer):
    """
    Wraps Stanza NER output for a specific policy.

    nlp_artifacts.entities is List[spacy.tokens.Span] — access via .label_,
    .start_char, .end_char.  Confidence scores live in the parallel list
    nlp_artifacts.scores (defaults to [0.85] * len(entities) when not provided
    by the NLP engine).  This recognizer filters by policy entity_types and
    min_score, then re-emits matches with entity_type=policy.id so results can
    be traced back to the originating policy.
    """

    def __init__(self, policy: NerEntityPolicy, language: str):
        super().__init__(
            supported_entities=[policy.id],
            name=f"{policy.id}__{language}",
            supported_language=language,
        )
        self._entity_types = set(policy.entity_types)
        self._min_score = policy.min_score

    def load(self) -> None:
        pass  # NER is handled by the NLP engine

    def analyze(
        self,
        text: str,
        entities: list[str],
        nlp_artifacts: NlpArtifacts,
    ) -> list[RecognizerResult]:
        if not nlp_artifacts:
            return []

        ner_spans = nlp_artifacts.entities or []
        # scores is a parallel list; Presidio defaults to 0.85 per entity when
        # the NLP engine does not provide confidence values.
        ner_scores = nlp_artifacts.scores or ([0.85] * len(ner_spans))

        results: list[RecognizerResult] = []
        for span, score in zip(ner_spans, ner_scores):
            if span.label_ not in self._entity_types:
                continue
            if score < self._min_score:
                continue
            results.append(
                RecognizerResult(
                    entity_type=self.supported_entities[0],  # policy id
                    start=span.start_char,
                    end=span.end_char,
                    score=score,
                )
            )
        return results


def _ner_recognizers(policy: NerEntityPolicy) -> list[NerPolicyRecognizer]:
    langs = [policy.language] if policy.language else _ALL_LANGS
    return [NerPolicyRecognizer(policy, lang) for lang in langs]
