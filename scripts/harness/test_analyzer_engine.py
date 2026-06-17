"""Unit tests for the confidence-scoring DLP engine (analyzer/engine.py).

Pure in-process tests — no orchestrator subprocess. conftest.py puts analyzer/
on sys.path, so `from engine import ...` resolves. Policies are written to a
tmp file per test so these don't depend on the shipped analyzer/policies.yaml.
"""
from __future__ import annotations

import time

from engine import DLPEngine, normalize_ws
from extractor import ColumnBlock, TabularData, is_tabular

# Single action mechanism (`actions`); context never gates, only boosts score.
# Denylist `conf` also declares context_words → same boost applies.
_SCORED = """
policies:
  - id: visa
    name: VISA
    channels: [browser, clipboard, peripheral_storage]
    type: regex
    patterns: ["\\\\b4\\\\d{3} ?\\\\d{4} ?\\\\d{4} ?\\\\d{4}\\\\b"]
    context_words: ["thẻ", "visa"]
    context_range: 40
    score_base: 0.5
    score_context_boost: 0.5
    actions:
      - {min_score: 1.0, action: block}
      - {min_score: 0.0, action: allow_log}
  - id: conf
    name: Confidential
    channels: [browser, clipboard, peripheral_storage]
    type: denylist
    keywords: ["nội bộ", "mật"]
    context_words: ["tài liệu"]
    context_range: 40
    score_base: 0.5
    score_context_boost: 0.5
    actions:
      - {min_score: 1.0, action: block}
      - {min_score: 0.0, action: allow_log}
"""


def _engine(tmp_path, yaml_text: str) -> DLPEngine:
    p = tmp_path / "policies.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    return DLPEngine(str(p))


def _matches(result, policy_id):
    for v in result.violations:
        if v.policy_id == policy_id:
            return v.matches
    return []


def _violation(result, policy_id):
    for v in result.violations:
        if v.policy_id == policy_id:
            return v
    return None


# --- detection / scoring (plain text) ---------------------------------------

def test_visa_detected_without_context_but_only_logged(tmp_path):
    eng = _engine(tmp_path, _SCORED)
    res = eng.analyze("xin gửi 4111 1111 1111 1111 nhe", "browser")
    ms = _matches(res, "visa")
    assert len(ms) == 1                       # detected (recall) even with no context
    assert ms[0].has_context is False
    assert ms[0].context_word is None
    assert ms[0].score == 0.5
    assert ms[0].action == "allow_log"
    assert res.applied_action == "allow_log"  # bare shape → not blocked


def test_visa_with_context_blocks_and_records_word(tmp_path):
    eng = _engine(tmp_path, _SCORED)
    res = eng.analyze("thẻ visa số 4111 1111 1111 1111", "browser")
    ms = _matches(res, "visa")
    assert len(ms) == 1
    assert ms[0].has_context is True
    assert ms[0].context_word in {"thẻ", "visa"}      # the triggering word is recorded
    assert ms[0].score == 1.0
    assert ms[0].action == "block"
    assert res.applied_action == "block"
    assert _violation(res, "visa").context_words      # aggregated on the violation


# --- whole-word keyword matching (Unicode boundary) + denylist context ------

def test_keyword_whole_word_positive(tmp_path):
    eng = _engine(tmp_path, _SCORED)
    res = eng.analyze("tài liệu lưu hành nội bộ và cần bảo mật", "browser")
    texts = {m.text for m in _matches(res, "conf")}
    assert "nội bộ" in texts          # standalone phrase
    assert "mật" in texts             # "mật" as a standalone syllable in "bảo mật"


def test_keyword_whole_word_negative(tmp_path):
    eng = _engine(tmp_path, _SCORED)
    # "mật" glued inside a longer token must NOT match (whole-word only).
    res = eng.analyze("matkhau mậtkhau abcmật", "browser")
    assert _matches(res, "conf") == []


def test_denylist_context_boost(tmp_path):
    eng = _engine(tmp_path, _SCORED)
    # keyword near its context word → block; the same keyword alone → allow_log.
    with_ctx = _matches(eng.analyze("tài liệu nội bộ", "browser"), "conf")
    assert with_ctx and with_ctx[0].action == "block"
    assert with_ctx[0].context_word == "tài liệu"
    without_ctx = _matches(eng.analyze("nội bộ", "browser"), "conf")
    assert without_ctx and without_ctx[0].action == "allow_log"
    assert without_ctx[0].context_word is None


# --- tabular: header/row/column context + performance -----------------------

def test_csv_is_tabular():
    assert is_tabular("x.csv") is True
    assert is_tabular("x.pdf") is False      # PDF routed to plain text


def test_tabular_header_context_boosts(tmp_path):
    eng = _engine(tmp_path, _SCORED)
    td = TabularData(columns=[
        ColumnBlock(header="Số thẻ VISA", values=["4111 1111 1111 1111"], sheet=None),
    ])
    ms = _matches(eng.analyze_tabular(td, "browser"), "visa")
    assert len(ms) == 1
    assert ms[0].has_context is True         # header "...thẻ...visa..." satisfies context
    assert ms[0].context_word in {"thẻ", "visa"}
    assert ms[0].action == "block"


def test_large_tabular_is_fast_and_correct(tmp_path):
    eng = _engine(tmp_path, _SCORED)
    n = 20000
    visa_col = ["4111 1111 1111 1111"] * n
    note_col = ["tài liệu nội bộ phục vụ lưu trữ"] * n   # keyword in every row
    td = TabularData(columns=[
        ColumnBlock(header="Số thẻ VISA", values=visa_col, sheet=None),
        ColumnBlock(header="Ghi chú", values=note_col, sheet=None),
    ])
    t0 = time.perf_counter()
    res = eng.analyze_tabular(td, "browser")
    elapsed = time.perf_counter() - t0
    assert len(_matches(res, "visa")) == n          # all detected
    assert elapsed < 5.0, f"analyze_tabular too slow: {elapsed:.2f}s"  # O(N), not O(N^2)


# --- free-text body: bounded proximity (NOT column/row context) -------------

def test_body_far_context_not_boosted(tmp_path):
    eng = _engine(tmp_path, _SCORED)
    # Context word "thẻ visa" is far (> visa context_range=40) from the value →
    # body proximity must NOT credit it (the bug this fix targets).
    filler = "loremipsum " * 12          # ~130 chars of non-matching prose
    td = TabularData(columns=[], body=[f"thẻ visa {filler} 4111 1111 1111 1111"])
    ms = _matches(eng.analyze_tabular(td, "browser"), "visa")
    assert len(ms) == 1                  # detected (recall preserved)
    assert ms[0].has_context is False
    assert ms[0].context_word is None
    assert ms[0].action == "allow_log"


def test_body_near_context_boosted(tmp_path):
    eng = _engine(tmp_path, _SCORED)
    td = TabularData(columns=[], body=["thẻ visa 4111 1111 1111 1111"])
    ms = _matches(eng.analyze_tabular(td, "browser"), "visa")
    assert len(ms) == 1
    assert ms[0].has_context is True
    assert ms[0].context_word in {"thẻ", "visa"}
    assert ms[0].action == "block"


def test_document_merges_table_and_body(tmp_path):
    eng = _engine(tmp_path, _SCORED)
    # A table cell with header context (→ block) AND a body value with no nearby
    # context (→ allow_log) must both appear in one merged violation.
    td = TabularData(
        columns=[ColumnBlock(header="Số thẻ VISA", values=["4111 1111 1111 1111"], sheet="Table 1")],
        body=["khách đã thanh toán 4242 4242 4242 4242 vào hôm qua"],
    )
    res = eng.analyze_tabular(td, "browser")
    ms = _matches(res, "visa")
    assert len(ms) == 2                                   # table + body, merged
    assert sorted(m.action for m in ms) == ["allow_log", "block"]
    assert res.applied_action == "block"                  # strongest action wins


def test_normalize_ws_heals_wrapped_pii(tmp_path):
    eng = _engine(tmp_path, _SCORED)
    # VISA split across a newline during extraction must still be detected.
    res = eng.analyze("thẻ visa 4111 1111\n1111 1111", "browser")
    assert len(_matches(res, "visa")) == 1
    assert normalize_ws("a \n b\t c") == "a b c"
