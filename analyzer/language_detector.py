"""
Language detection restricted to English and Vietnamese using Lingua.

Lingua with only 2 languages loads minimal models (few dozen MB) and
is accurate for short/ambiguous text, making it suitable for 500-word chunks.
"""

from __future__ import annotations

from lingua import Language, LanguageDetectorBuilder

_SUPPORTED = [Language.ENGLISH, Language.VIETNAMESE]

_detector = LanguageDetectorBuilder.from_languages(*_SUPPORTED).build()


def detect(text: str, default: str = "vi") -> str:
    """
    Return 'en' or 'vi' based on the text content.
    Falls back to `default` when Lingua cannot determine the language
    (e.g. very short, numeric-only, or truly mixed input).
    """
    lang = _detector.detect_language_of(text)
    if lang is Language.VIETNAMESE:
        return "vi"
    if lang is Language.ENGLISH:
        return "en"
    return default
