# Fix the DLP Analyzer: verify.py count parity, confidence-scored context, no timeouts

## Context

The endpoint DLP agent routes intercepted content (peripheral-storage copies, browser uploads,
clipboard text) to an **in-process** Python analyzer (the orchestrator calls `engine.analyze()` /
`engine.analyze_tabular()` directly on a `ThreadPoolExecutor`, 4.0 s timeout — `dispatcher.py:19`).
Detection is **RE2 regex (PII) + Aho-Corasick keyword matching** — *not* Presidio/Stanza (that
memory note is stale). Two defects, reproduced on `tmp/final-demo/deny` (8 formats × brackets
B1/B2/B3; PII per type = 10/30/80; oracle = `verify.py`):

1. **Reported counts ≠ verify.py.** e.g. `csv_b1` 1/1/1 (exp 10/10/10), `txt_b1` 10/10/**7**,
   `docx_b1` 10/10/**8**, `pdf_b1` 10/**6/6**.
2. **Timeouts everywhere.** Tabular analysis is **5–23 s on the ~750 KB B1 files** (docx 19 s,
   odt 23 s, ods 14 s, xlsx 7 s, pdf 5 s) vs the 4 s timeout → every tabular file fails closed
   *by timeout*, not real detection. Plain text (csv/txt/md) = 14–15 ms.

### Root causes (all confirmed empirically this session)
- **RC1 — Context used as a hard GATE.** VISA/CCCD/phone regex policies require a context keyword
  near each value (VISA ±120, CCCD ±200, phone ±100 chars; `policies.yaml`); `verify.py` counts
  unconditionally. The corpus puts ~half of each file's PII in **unlabeled body filler** with no
  nearby keyword. Proven: dropping the gate makes the analyzer reproduce `verify.py` **exactly on
  all 24 files** — every file now reads exactly 10/30/80 for VISA/CCCD/phone.
  (Correction: an earlier note blamed a CSV phone gap on Excel stripping the leading `+`. That was
  a false alarm from a `grep -o '+84'` artifact — the bare `+` is misparsed. Verified via raw byte
  count + `grep -oF` + the csv reader: `csv_b3` genuinely has 27 `+84` phones and reads phone=80.
  Opening the corpus in Excel did **not** corrupt counts. Still, treat it as read-only: Excel *can*
  round 16-digit VISA to 15 sig-figs / drop leading zeros if it ever re-saves.)
- **RC2 — CSV mis-routed to plain text.** `.csv` is missing from `_TABULAR_SUFFIXES`
  (`extractor.py:51`) → a header'd CSV is scanned as one blob where the only context is the single
  header row → 1/1/1. `extract_tabular()` already implements `.csv`.
- **RC3 — Tabular O(N×hits) hot loop (the timeout).** Every Aho-Corasick keyword hit runs
  `col_text[:start_idx].count("\n")` **twice** (`engine.py:250,256`) — O(N) from the start of a
  multi-hundred-KB joined column, ×thousands of hits.
- **RC4 — Slow structured extraction.** PyMuPDF `find_tables` = **45 s** on the 8 MB PDF (vs
  0.76 s for `get_text`); odfpy = **16–17 s** for ODS; python-docx = **5.4 s** for DOCX.
- **RC5 — Keyword substring over-match.** Keywords match as *substrings* → `"mật"`/`"nội bộ"`
  fire 3000+×/small file inside ordinary Vietnamese words. (Whole-word only partly helps — these
  are common standalone syllables; the real perf fix is RC3, and whole-word still cuts the
  in-word false positives.)
- **RC6 — Whole-word keyword matching must be Unicode-correct (impl detail, not an admin knob).**
  Aho-Corasick matches plain substrings, so to honor "whole-word only" we add our own boundary
  check after each hit: accept only if the chars just before/after are non-alphanumeric. Use Python
  `str.isalnum()` (Unicode-aware → `ậ`,`ộ` count as letters), **not** RE2 `\b` (ASCII only → would
  see a false boundary inside `mật`). Separately, RE2 has **no look-behind**, so the PII regexes
  keep `\b` boundaries rather than porting `verify.py`'s `(?<!\d)…(?!\d)` (copying those is a
  compile error: `invalid perl operator: (?<!`, verified).
- **RC7 — No standalone tester in use.** `test_cli.py` is abandoned (latent
  `--channel peripheral` vs `peripheral_storage` bug). We build a fresh `iso_test.py` instead of
  reviving it.
- **RC8 — Dead size limits.** `max_clipboard_bytes`/`max_file_bytes` are loaded into
  `OrchestratorConfig` (`config.py:17-18,86-87`) but **never read anywhere** — dead config today.

### Design decisions (confirmed with the user this session)
- **Confidence-scoring model** (industry standard — Presidio `LemmaContextAwareEnhancer`, Palo Alto
  Enterprise DLP, MS Purview): a shape match is **always** detected + counted (= verify.py recall);
  **context only raises a confidence score / stronger action — it never drops a match**. Admins
  compose actions per score band. Every match records **`has_context`** + score + action.
- **Skip structural validation** (no Luhn/province/prefix): it hard-codes per-PII-type logic, which
  cuts against the engine's generic, admin-configurable regex design (VISA/CCCD/phone are only demo
  policies). Score = `base + context_boost`; **no `validated` field**.
- **Context must work for tables**, not just prose proximity: a cell's context may be its column
  header, a row header, or another cell in the same row/column. Keep + perf-fix the tabular path.
- **Whole-word** keyword matching.
- **Size limits:** enforce them, **fail behavior configurable per channel, default fail-closed**,
  and the reason (`timeout` vs `size_limit`) must appear in the log.
- **Isolated tester** must print counts like the real agent **and** emit an artifact with the
  triggering policy id (+ `has_context`) next to every match.

### Confidence-scoring specifics (target behavior)
Per match: `score = score_base (+ score_context_boost if has_context)`, range **[0.0, 1.0]**
(Presidio convention). **Every policy uses the same defaults** — `score_base = 0.5`,
`score_context_boost = 0.5` — so a match is always one of two clean bands: **no-context = 0.5,
has-context = 1.0** (validation is skipped, so there is no third value). Policy maps score → action
(thresholds, admin-configurable); overall verdict = strongest action (existing `_strongest_action`,
generalized). `has_context`:
- **prose:** a context keyword within ±`context_range` chars (proximity).
- **tabular:** a context keyword in the **same row OR same column (incl. header)** — richer than
  today's header-only check; precompute per-row/per-column keyword presence (O(cells) once),
  O(1) per match.
Recall (what's counted) is independent of score, so verify.py parity holds.

---

## Implementation tasks (in order)

### ✅ DONE — Task 1 — Confidence model in the engine (`analyzer/engine.py`, `analyzer/policy.py`, `analyzer/policies.yaml`)
> Implemented + validated: Match carries has_context/score/action; context never gates (scored policies); legacy policies keep gating for back-compat. policies.yaml on the new schema (base 0.5, boost 0.5, action ladder; redundant clipboard CCCD policy collapsed).
- `Match` gains `has_context: bool` and `score: float`; `Violation`/`AnalysisResult` carry them through.
- **Remove context as a filter.** In `analyze()` and `analyze_tabular()`, always append shape
  matches; compute `has_context` separately and set `score`.
- `policy.py` + `policies.yaml`: per policy add `score_base` (default 0.5), `score_context_boost`
  (default 0.5 — same for every policy), and an action mapping by threshold, e.g.
  `actions: [{min_score: 1.0, action: block}, {min_score: 0.0, action: allow_log}]` (context → block,
  bare shape → log; or set `min_score: 0.5` for block to make everything block).
  Keep VISA/CCCD/phone on all three channels; collapse the redundant
  `block_cccd_clipboard_no_context` into the single CCCD recognizer (its purpose — "bare value with
  no context still handled" — is now the no-context score band).
- **Pre-test:** `DLPEngine('policies.yaml')` loads; a value with vs without a nearby keyword yields
  the same detection but different `has_context`/`score`/action.

### ✅ DONE — Task 2 — CSV routing + table-aware context (`analyzer/extractor.py`, `analyzer/engine.py`)
> Implemented + validated: `.csv` added to tabular suffixes; analyze_tabular now sets has_context from header OR same-column OR same-row context.
- Add `.csv` to `_TABULAR_SUFFIXES` (`.tsv` already present); `extract_tabular()` already handles it.
- Implement same-row / same-column / header context for `has_context` in `analyze_tabular` (precompute
  row-has-keyword and column-has-keyword sets from the Aho-Corasick hits).
- **Pre-test:** `is_tabular('x.csv')` True; a CCCD under a `Số CCCD` header gets `has_context=True`
  even though the header is far away.

### ✅ DONE — Task 3 — Performance: row-map, whole-word keywords, whitespace (`analyzer/engine.py`)
> Implemented + validated: O(N) newline-count replaced by accumulate+bisect row map; whole-word keywords via str.isalnum() boundary; whitespace normalized for plain. All 24 corpus files now 10/30/80 with max elapsed ~7.9 s (was 5–47 s).
- Replace the O(N) `col_text[:start_idx].count("\n")` (`:250,256`) with precomputed cell-start
  offsets (`itertools.accumulate(len(v)+1 …)`) + `bisect` → O(rows) build, O(log rows) per hit.
- Whole-word keywords: accept an Aho-Corasick hit only if the char before `start` and at `end` are
  non-alphanumeric per Python `str.isalnum()` (Unicode-correct for Vietnamese). **Not** RE2 `\b`.
- Normalize whitespace (`\s+`→" ") before plain-text scanning to heal wrapped PII (matches
  `verify.py`); annotate the normalized text in Task 5.
- **Pre-test:** analyze B1–B3; per-type counts == verify.py and elapsed well under 4 s.

### ✅ DONE — Task 4 — Fast extraction + timeout + size limits (`analyzer/extractor.py`, `orchestrator/policy_manager.py`, `orchestrator/dispatcher.py`, `orchestrator/config.py`, `config.yaml`)
> Implemented + validated: lxml streaming for ods/odt/docx (16.5s→3.6s etc.); PDF routed to plain get_text; xlsx keeps openpyxl. Timeout config-driven (`service.analysis_timeout_seconds`, default 4.0 for harness, 10 in config.yaml); client pipe waits raised to 12 s; drain 12 s. Size caps enforced in policy_manager with per-channel `oversize_fail_behavior` (default block) and `reason=size_limit` log; timeout path logs `reason=timeout`.
- Replace slow structured extractors with **lxml streaming** that preserves columns (validated:
  ODS B3 16.5 s→3.6 s, DOCX B3 5.4 s→2.0 s; ODT mirrors ODS). DOCX: exclude `w:p` with a `w:tbl`
  ancestor to avoid double-counting cell paragraphs. Keep `openpyxl` (read_only) for XLSX (~3 s,
  correct) and the stdlib `csv` reader for CSV.
- **PDF:** `find_tables` (45 s) is untenable. **Recommended: extract plain text via `get_text`
  (~0.8 s)** — recall is unaffected (no-context band still detects + counts every value); PDF table
  cells get best-effort *proximity* context instead of header context. *(User deferred the PDF
  call; see "Open item" — flip to capped `find_tables` if you want header context on PDF tables.)*
- **Timeout → 10 s, kept consistent across the IPC chain.** Invariant: every client's pipe-wait
  must be **>** the orchestrator's analysis timeout, or the client gives up before the orchestrator
  answers. Make the orchestrator timeout config-driven (add `service.analysis_timeout_seconds: 10`
  to `config.yaml` + `OrchestratorConfig`; `dispatcher.py:19` reads it instead of the hardcoded
  `4.0`). Then in `config.yaml` raise the (config-driven, no C# rebuild needed) client waits above
  10 s: `clipboard.pipe_timeout_ms` 6000→**12000**, `browser.pipe_timeout_seconds` 5→**12**,
  peripheral `…transfer_agent.analysis_timeout_seconds` 10→**12**, and `service.drain_timeout_seconds`
  8→**12** (so in-flight analyses can finish on shutdown). 10 s leaves debugging headroom; real
  analysis is <4 s after the perf fixes.
- **Size limits:** enforce `max_file_bytes`/`max_clipboard_bytes` in `policy_manager.analyze()`;
  oversized input short-circuits to a per-channel **configurable** verdict (default **BLOCK**,
  fail-closed) **without** extracting, and the log records reason=`size_limit`. Also tag the
  timeout path with reason=`timeout` (`dispatcher.py`). Add a `fail_behavior` map per channel in
  `config.yaml`; thread the limits/behavior into `PolicyManager`.
- **Pre-test:** every B3 file end-to-end under 10 s (well under); oversized + timeout paths log the reason.

### ✅ DONE — Task 5 — Standalone tester with annotated output (new top-level `manual_test/iso_test.py`)
> Implemented + run: `manual_test/iso_test.py` → all 24 deny files PASS (analyzer == oracle == expected 10/30/80), max ~7.4 s. Outputs `manual_test/iso_test_out/{summary.txt, matches.csv (2880 PII rows), <stem>.annotated.txt / <stem>.extracted.txt}`. Confidential keyword matches summarized as a count by default; `--all` includes them per-match.
- **Lives in a new `manual_test/` folder at the repo root — NOT inside the `analyzer` package.** It
  imports the analyzer modules (adds `analyzer/` to `sys.path`) but stays out of the shipped code.
- Runs in the venv against `tmp/final-demo/deny` (no orchestrator/pipes). Per file: extract →
  analyze → per PII **type**, count **deduped** matches, compare to the `verify.py` oracle regexes
  and to expected 10/30/80; print a PASS/FAIL table with `elapsed_ms`.
- **Output layout** (all under one dir, `--out`, default `manual_test/iso_test_out/`; all files UTF-8):
  - **`matches.csv` — ONE file for the whole run** (not per-file): one row per match, columns
    `file, format, location, type, value, policy_id, has_context, score, action`. The `file` column
    distinguishes sources, so you can filter/sort all matches in one place. (This is the primary
    spot-check artifact.)
  - **`summary.txt` — ONE file**: the PASS/FAIL counts table (also printed to stdout).
  - **One annotated text file PER scanned file:**
    - plain text (`txt`/`md`) → `<stem>.annotated.txt` = the (whitespace-normalized) extracted text
      with `⟦policy_id|context=yes/no⟧` inserted right after each matched span (faithful, char-offset
      based).
    - office/PDF/csv → `<stem>.extracted.txt` = a readable dump of the *extracted* content (one
      cell/paragraph per line, location-prefixed), with the same `⟦…⟧` tag after each match — e.g.
      `[sheet=Sheet1 | col="Số CCCD" | row=42] 046203470173 ⟦cccd|context=yes⟧`. (Binaries can't be
      rewritten inline, so this stands in for an annotated original.)
- `test_cli.py` is **not** reused (optionally delete it for one clear entry point).
- **Pre-test:** run the tester; confirm `summary.txt`, the single `matches.csv`, and one annotated
  file per input appear in the out dir.

### ✅ DONE — Task 6 — Audit log field + regression tests (`orchestrator/events.py`, `scripts/harness/` pytest)
> Implemented + run: events.jsonl per-violation now carries `action` + `with_context` (count of context-confirmed matches); `scripts/harness/test_analyzer_engine.py` added (9 passed). Full relevant harness suite green: test_analyzer_engine 9, test_events 7, test_{timeout,concurrency,supersession,hot_reload} 7, test_{supervisor,ctl_pipe,admin} 7 passed/3 skipped — no failures (back-compat preserved).
- Add `has_context` (and score/action) to the per-violation audit records in `events.py`.
- Automated pytest goes in **`scripts/harness/`** (the repo's existing pytest home with `conftest.py`)
  — e.g. `scripts/harness/test_analyzer_engine.py`, **not** inside the `analyzer` package: no-context
  detection per type; context boost (prose proximity + tabular row/col/header); whole-word keyword
  positive/negative; CSV routed tabular; synthetic large tabular completes fast. Keep tests
  independent of the big corpus.

---

## Post-implementation changes — ROUND 2 — ✅ DONE + verified (24/24 PASS, pytest 20 passed)

Three follow-ups requested after round 1 landed. Decisions confirmed with the user; DLP research
backs PI-2/PI-3 (Skyhigh/Purview/Palo Alto: keyword+proximity cuts FPs; audit logs should carry
investigation context but never the raw sensitive value — the context word is a generic term, safe).

### ✅ PI-1 — One action field: remove `action`, keep only `actions`
> Done: `Policy.action` + `scored` removed; engine always scores (no gate); all `action:` keys gone from policies.yaml + visa_block fixture; `_LEGACY` test removed. Grep confirms no `policy.action`/`.scored` refs remain.
Two action fields (`action` + `actions`) are confusing. Make `actions` the sole mechanism.
- `analyzer/policy.py`: delete `Policy.action` and the `scored` flag. `resolve_action(score)` returns
  the ladder match or `"allow"` floor. `load_policies` stops reading `entry["action"]`
  (keep `score_base`/`score_context_boost`). `actions` defaults to `[(0.0,"allow")]` if absent.
- `analyzer/engine.py`: `_score_and_action` always uses the confidence path (drop the legacy branch
  and the `keep` return) → **context never gates**; every shape match is detected+scored. A
  "require context" policy is now expressed in the ladder (no-context band → `allow`).
- `analyzer/policies.yaml`: remove every `action:` line; each policy keeps `actions:` (confidential
  gains an `actions` ladder via PI-2).
- Fixtures/tests: migrate `scripts/harness/fixture_policies/visa_block.yaml` to `actions:`
  (floor `block`) — it's used by `test_hot_reload`; remove the `_LEGACY` fixture +
  `test_legacy_context_gate_drops_bare_match` and the `action:` keys from `_SCORED` in
  `scripts/harness/test_analyzer_engine.py`.
- **Unchanged:** `Violation.action` / `Match.action` (the RESOLVED output action) stay — only the
  policy-config field is removed. Only the confidential + fixture policies were ever legacy and none
  used context-as-a-gate, so no runtime behavior changes for existing policies.

### ✅ PI-2 — Context mechanism for denylist + seed an example (user: "Yes, and seed an example")
> Done: confidential policy seeded with example context_words (tài liệu/văn bản/phân loại/đóng dấu) + actions ladder (keyword→allow_log, keyword+context→block). Verified by test_denylist_context_boost.
The engine already computes `has_context` for any denylist policy that declares `context_words`
(via the unified model), so no engine change. In `analyzer/policies.yaml`, give
`block_confidential_keywords` illustrative `context_words` (e.g. "tài liệu", "văn bản", "phân loại",
"đóng dấu") + `score_base`/`score_context_boost`/`actions` so a keyword near such context escalates
(context → `block`, bare keyword → `allow_log`). Marked admin-tunable in comments.

### ✅ PI-3 — Surface the triggered context word (user: test=per-match word; events+agent log=per-policy word set)
> Done: Match.context_word (per-match) + Violation.context_words (per-policy set); matches.csv has a populated context_word column + annotations show context=<word>; events.jsonl per-violation has context_words; dlp-agent.log violation summary appends [ctx:...].
- `analyzer/engine.py`: `_has_context_proximity` → returns the matched context word (`str | None`);
  tabular `col_context`/`row_context` store the word (header/column/row). `Match` gains
  `context_word: str | None` (`has_context = context_word is not None`); `Violation` gains
  `context_words: list[str]` = sorted-unique words across its matches.
- `manual_test/iso_test.py`: add a `context_word` column to `matches.csv` (per-match word);
  annotations show `⟦policy_id|context=<word>⟧` (or `context=no`).
- `orchestrator/events.py` + `dispatcher._emit_event`: per-violation record adds `context_words`
  (the set of words triggered for that policy — not per-match).
- `orchestrator/policy_manager._fmt_violations`: append `[ctx:w1,w2]` per policy to the BLOCK/ALLOW
  `dlp-agent.log` line.
- Tests: `test_events.py` stub gains a `context_words` attr; update the violation-dict assertions.
  `test_analyzer_engine.py` asserts `context_word` on a context-boosted match + a denylist-with-context case.

### Verification (round 2)
- `python manual_test/iso_test.py --corpus tmp/final-demo/deny` → all 24 PASS; `matches.csv` has a
  populated `context_word` column.
- `python -m pytest scripts/harness/test_analyzer_engine.py scripts/harness/test_events.py scripts/harness/test_hot_reload.py scripts/harness/test_timeout.py` → green.
- Grep confirms **no** `action:` key remains in `analyzer/policies.yaml` or `fixture_policies/`, and
  **no** `Policy.action` / `.scored` references remain in `analyzer/` or `orchestrator/`.

---

## Verification

### A. Dev machine, analyzer-only — ✅ COMPLETED THIS SESSION (all green)
Prereq (verified this session): `D:/Code/GithubPublishEndpointDLP/.venv/Scripts/python.exe` is
Python 3.13 and imports `re2, ahocorasick, yaml, lxml, docx, openpyxl, odf, fitz`.
1. From repo root: `…/.venv/Scripts/python.exe manual_test/iso_test.py --corpus tmp/final-demo/deny`
   → every file **PASS** (analyzer per-type counts == verify.py oracle; CSV phone is the known
   `+`-less Excel quirk the oracle shares — restore a pristine CSV for 80), `elapsed_ms` second-range
   for all 24 files (far under the 10 s timeout).
2. Inspect `manual_test/iso_test_out/`: the single `matches.csv` + a per-file annotated/extracted
   `.txt` — each match shows its `policy_id` + `has_context`.
3. `…/.venv/Scripts/python.exe -m pytest scripts/harness/test_analyzer_engine.py` — green.

### B. VM end-to-end (after A passes) — per `README.md`
Build/bundle/deploy to the clean Win11 VM as documented, `Restart-Service`, `dlp-ctl status`, then
copy a `deny` file to a removable drive / upload via browser → fast **BLOCK**; an `allow` file →
**ALLOW**. Confirm no `reason=timeout` entries in the event log.

---

## Answers to the user's earlier questions
- **`max_file_bytes` unused?** Correct — `max_clipboard_bytes`/`max_file_bytes` are parsed into
  `OrchestratorConfig` and sourced from `config.yaml`, but a repo-wide search finds them referenced
  **only** in `config.py` and test fixtures — never in `dispatcher.py`/`server.py`/`policy_manager.py`.
  Today an arbitrarily large file is extracted and analyzed regardless. Task 4 wires them in.
- **Annotated output possible?** Yes for plain text (faithful inline tags); for office/PDF we
  annotate the extracted rendering + a universal `matches.csv` (the binaries can't be safely
  rewritten inline).

## Open item for you
- **PDF**: recommendation is plain-text extraction (fast; full recall; proximity-only context on
  PDF tables). Say the word if you'd rather keep header-aware PDF tables via **capped** `find_tables`
  (accurate but needs a page/size guard to avoid the 45 s hang).

## Feasibility / nothing blocked
- All approaches were prototyped this session and run. One watch-point: ODS B3 (11 MB) structured
  extraction is ~3.6 s — under the 4 s timeout but the tightest; optimizable, and the size cap
  guards larger files. No task is blocked. All commands use the verified venv interpreter.
