"""
Core analyzer service.

Responsibilities:
  - Load Stanza NLP models for English and Vietnamese on startup.
  - Build a Presidio AnalyzerEngine populated with recognizers derived from policies.
  - For each incoming chunk request: detect language, filter applicable policies by
    channel, run analysis, map results back to policies, compute applied action.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider

import language_detector
from policy import Action, DenylistPolicy, NerEntityPolicy, Policy, RegexPolicy, load_policies, strongest_action
from recognizer_factory import build_recognizers

log = logging.getLogger(__name__)

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
    detected_language: str
    applied_action: Action
    violations: list[Violation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "detected_language": self.detected_language,
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
    def __init__(self, config: dict[str, Any], policies: list[Policy]):
        self._default_language: str = config.get("default_language", "vi")
        self._policy_map: dict[str, Policy] = {p.id: p for p in policies}

        log.info("Loading Stanza NLP models (this may download models on first run)...")
        nlp_engine = _build_nlp_engine(config)

        # Use an empty registry — all detection is driven by our policy recognizers.
        # Presidio's built-in recognizers (email, phone, etc.) are not loaded because
        # they use their own entity type names that won't match any policy id, and
        # we filter analysis to only our entity_ids anyway.
        registry = RecognizerRegistry(recognizers=[], supported_languages=["en", "vi"])
        for recognizer in build_recognizers(policies):
            registry.add_recognizer(recognizer)

        self._engine = AnalyzerEngine(
            nlp_engine=nlp_engine,
            registry=registry,
            supported_languages=["en", "vi"],
        )

        # Timing state — overwritten each request (safe because connections are serialized)
        self._nlp_time_ms: float = 0.0
        self._recognizer_timings: dict[str, tuple[int, float]] = {}  # name → (hits, ms)
        self._install_timing_hooks()

        log.info("AnalyzerEngine ready.")

    def _install_timing_hooks(self) -> None:
        """Monkey-patch the NLP engine and each recognizer to record per-call timing."""

        # --- NLP engine ---
        nlp_engine = self._engine.nlp_engine
        _original_process = nlp_engine.process_text

        def _timed_process(text: str, language: str):
            t0 = time.perf_counter()
            result = _original_process(text, language)
            self._nlp_time_ms = (time.perf_counter() - t0) * 1000
            return result

        nlp_engine.process_text = _timed_process

        # --- Each recognizer ---
        timings = self._recognizer_timings
        for recognizer in self._engine.registry.recognizers:
            _orig_analyze = recognizer.analyze
            _name = recognizer.name

            def _make_timed(orig, name):
                def _timed_analyze(text, entities, nlp_artifacts):
                    t0 = time.perf_counter()
                    results = orig(text, entities, nlp_artifacts)
                    elapsed = (time.perf_counter() - t0) * 1000
                    timings[name] = (len(results), elapsed)
                    return results
                return _timed_analyze

            recognizer.analyze = _make_timed(_orig_analyze, _name)
            log.info("Loaded recognizer: %s", recognizer.name)

    def analyze(self, request: dict[str, Any]) -> AnalysisResult:
        chunk_id: str = request.get("chunk_id", "")
        text: str = request.get("text", "")
        metadata: dict = request.get("metadata", {})
        channel: str = metadata.get("channel", "")

        # Detect language; fall back to configured default
        _t_lingua = time.perf_counter()
        detected_lang = language_detector.detect(text, default=self._default_language)
        lingua_ms = (time.perf_counter() - _t_lingua) * 1000

        # Policies applicable to this channel
        active_policies = [
            p for p in self._policy_map.values()
            if not p.channels or channel in p.channels
        ]
        # Exclude NER policies registered for the other language — Presidio would
        # warn "Entity X doesn't have the corresponding recognizer in language: Y"
        # for each one, since we only register NerPolicyRecognizer per declared language.
        entity_ids = [
            p.id for p in active_policies
            if not isinstance(p, NerEntityPolicy)
            or p.language is None
            or p.language == detected_lang
        ]

        if not entity_ids:
            return AnalysisResult(
                chunk_id=chunk_id,
                detected_language=detected_lang,
                applied_action="allow_no_log",
            )

        self._recognizer_timings.clear()
        _t_presidio = time.perf_counter()
        presidio_results = self._engine.analyze(
            text=text,
            language=detected_lang,
            entities=entity_ids,
        )
        presidio_total_ms = (time.perf_counter() - _t_presidio) * 1000
        recognizers_ms = presidio_total_ms - self._nlp_time_ms

        if log.isEnabledFor(logging.DEBUG):
            log.debug("chunk=%s lang=%s", chunk_id[:8], detected_lang)
            log.debug("  lingua_detect: %6.1fms", lingua_ms)
            log.debug("  stanza_nlp:  %7.1fms", self._nlp_time_ms)
            log.debug("  recognizers: %7.1fms  (total)", max(recognizers_ms, 0.0))
            for name, (hits, ms) in sorted(
                self._recognizer_timings.items(), key=lambda x: -x[1][1]
            ):
                hit_label = f"{hits} hit{'s' if hits != 1 else ''}"
                log.debug("    %-40s %6s  %5.1fms", name, hit_label, ms)

        # Group results by policy id
        groups: dict[str, list] = {}
        for r in presidio_results:
            groups.setdefault(r.entity_type, []).append(r)

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
            detected_language=detected_lang,
            applied_action=applied,
            violations=violations,
        )


# ---------------------------------------------------------------------------
# NLP engine construction
# ---------------------------------------------------------------------------

def _build_nlp_engine(config: dict[str, Any]):
    models = config.get("stanza_models", [
        {"lang_code": "en", "model_name": "en"},
        {"lang_code": "vi", "model_name": "vi"},
    ])
    nlp_configuration = {
        "nlp_engine_name": "stanza",
        "models": models,
        "ner_model_configuration": {
            "model_to_presidio_entity_mapping": {
                # English CoNLL / OntoNotes labels
                "PER": "PERSON",
                "PERSON": "PERSON",
                "ORG": "ORGANIZATION",
                "ORGANIZATION": "ORGANIZATION",
                "LOC": "LOCATION",
                "LOCATION": "LOCATION",
                "GPE": "LOCATION",
                "FAC": "LOCATION",
                # Vietnamese VLSP labels
                "MISCELLANEOUS": "NRP",
            },
            "low_confidence_score_multiplier": 0.4,
            "low_score_entity_names": ["ORGANIZATION"],
            "labels_to_ignore": ["CARDINAL", "ORDINAL", "QUANTITY", "PERCENT",
                                  "MONEY", "WORK_OF_ART", "EVENT", "PRODUCT"],
        },
    }
    provider = NlpEngineProvider(nlp_configuration=nlp_configuration)
    return provider.create_engine()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_service(config_path: str, policies_path: str) -> AnalyzerService:
    import yaml, os
    if not os.path.exists(config_path):
        config: dict = {}
    else:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

    policies = load_policies(policies_path)
    return AnalyzerService(config, policies)
