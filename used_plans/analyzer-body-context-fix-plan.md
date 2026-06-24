# Fix: free-text body in DOCX/ODT must use proximity context, not tabular context

**Status: IMPLEMENTED + verified on the dev machine (2026-06-17). All commands below were
pre-run successfully before this document was handed over.**

This is a **separate** plan from `analyzer-fix-and-test-plan.md` (the earlier counts/timeout/
confidence-scoring fix). Do not confuse the two.

---

## 1. Problem (what you reported)

For the `*_v4` corpus files (`D:\Code\testfile_forDLP\analyzer-test`), the analyzer mis-attributed
context in DOCX/ODT but not in the PDF of the same content. Concretely, the bare phone
`0987654321` (which has **no** phone context word within range) was being credited with
`context = "số điện thoại"` in docx/odt, while the PDF correctly reported `context = no`.

You diagnosed that "tabular vs plain analysis chosen purely by file extension" is the problem, and
proposed running **both** modes on document files (tables → tabular, prose → plain proximity),
worrying about (a) a complexity penalty and (b) the proximity scan bleeding context across
paragraphs. You also weren't sure whether the lxml extractor distinguishes paragraphs from tables.

## 2. Root cause (confirmed empirically, not theory)

**Term — "body":** the prose paragraphs of a document that belong to *no* table.
**Term — "tabular context":** a match is credited with context if a context word is in its column
header, anywhere in its column, or anywhere in its row.
**Term — "proximity context":** a match is credited with context only if a context word is within
±`context_range` **characters** of it.

- The lxml extractor **already separates** tables from prose. `extract_tabular()` on a docx/odt put
  table cells into table columns (header + `sheet="Table N"`) and put body paragraphs into a single
  catch-all column with `header=""`, `sheet=None`, *one paragraph per value*. (Verified: `docx_v4`
  is pure prose → 1 column, 6 values; all PII sits in the 395-char paragraph.)
- The bug was purely in the **engine**: `analyze_tabular()` resolved context for *every* column —
  including that prose body column — with **tabular context**. `col_context[(policy_id, col_idx)]`
  is set if a context word appears *anywhere in the column*, then applied to *every* hit in the
  column. For prose, "the column" is the whole document → context became effectively **unbounded**.
- Result on `docx_v4` body (current `analyze_tabular` → corrected proximity):
  - phone `0987654321`: `ctx=số điện thoại` → **`ctx=None`** (action unchanged — phone is always
    `allow_log`).
  - confidential keyword `mật` ("…cần được bảo mật theo tiêu chuẩn ISO…"): `ctx=văn bản → block`
    → **`ctx=None → allow_log`**. This was a genuine **false-positive block**: the unrelated word
    "văn bản" elsewhere in the document falsely escalated it.
  - VISA/CCCD ×4: unchanged (their real context words are genuinely nearby) → still `block`.
- **The PDF path was already correct** because `.pdf` routes to plain `extract_text` + `analyze`
  (proximity). The defect was that docx/odt diverged from their own PDF twin.

So recall (counts) was never wrong; the file-level verdict for this corpus was never wrong (VISA/CCCD
dominate). What was wrong: per-match context attribution in prose, **and** a latent false-positive
block for any document where a bare VISA/CCCD/keyword's only context word is farther than
`context_range` away.

## 3. How the industry solves this (web-researched)

- **Microsoft Purview** matches supporting keywords within a **character-proximity window, default
  300 chars**; "Anywhere in the document" is an explicit, non-default opt-in. (Our old body behavior
  was effectively that opt-in, applied by accident.)
- **Microsoft Presidio** (`LemmaContextAwareEnhancer`) boosts confidence only when a context word is
  in the **surroundings** (before/after) of the entity — proximity, never whole-document.

Conclusion: **bounded character proximity is the standard for free text**; header/same-row context
is the correct model only for genuinely structured records. Your proposal *is* the industry approach.

## 4. Decisions (confirmed with you this session)

1. **Body proximity = Path A:** concatenate body paragraphs and run `analyze()` once. Context may
   reach an adjacent paragraph if within ±`context_range`, exactly like the same document's PDF/TXT
   twin and Purview's default. Chosen over strict per-paragraph because it keeps all four formats
   **consistent** and is the simplest (reuses `analyze()` verbatim).
2. **PDF unchanged.** It already uses the correct plain proximity path; fix scope = DOCX/ODT (+ the
   shared plumbing). PDF table header-context (capped `find_tables`) is explicitly out of scope.
3. **Proximity-gate prose:** a bare VISA/CCCD in prose whose only context word is beyond range scores
   0.5 → `allow_log` (logged, not blocked) in docx/odt — same as TXT/PDF already behave. Accepted.

### On your two worries — both resolved
- **Complexity penalty ≈ zero.** The Aho-Corasick automaton is built once at engine init. The body
  is scanned exactly once (by `analyze`); table columns are scanned once (by the existing tabular
  loop); the two cover **disjoint** content — no double scan, no O(N²). Measured: docx/odt v4 ≈ 0 ms.
- **Bleed is bounded and matches the PDF.** Proximity is hard-capped at `context_range` (≤200 chars),
  so a context word can only reach the immediately-adjacent sentence, never a "far away" paragraph —
  and the PDF twin already behaves identically.

## 5. Design

A document model = **structured table columns + free-text body**, analyzed each with its own context
method, then merged per policy.

- `TabularData` gains `body: list[str]` (paragraphs). Extractors populate it instead of appending a
  `header=""` body column. Pure spreadsheets (csv/tsv/xlsx/ods) leave `body` empty.
- `analyze_tabular()` keeps the existing column logic, then — if `tabular.body` is non-empty — runs
  `self.analyze("\n".join(body), channel)` (the plain proximity engine) and merges its violations
  into the tabular ones by policy id via a new `_merge_violations()`. No call site changes
  (`policy_manager`, `iso_test`, tests all keep calling `analyze_tabular`).

## 6. Implementation tasks (done in this order)

### ✅ Task 1 — `analyzer/extractor.py`: split body out of columns
- `TabularData` gains `body: list[str] = field(default_factory=list)` with a docstring.
- `_extract_docx_tabular`, `_extract_odt_tabular`, `_extract_pdf_tabular`: return
  `TabularData(columns=columns, body=body)` instead of appending a `header=""` body `ColumnBlock`.
- Generic fallback in `extract_tabular`: `TabularData(columns=[], body=text.splitlines())`.
- CSV/TSV/XLSX/ODS extractors: unchanged (`body` defaults empty).

### ✅ Task 2 — `analyzer/engine.py`: proximity on body + merge
- At the end of `analyze_tabular`, before computing `applied_action`:
  `if tabular.body: violations = self._merge_violations(violations, self.analyze("\n".join(tabular.body), channel).violations)`.
- New `_merge_violations(a, b)`: unions matches by `policy_id` (tabular col/row matches and plain
  start/end matches reference disjoint content and are each already deduped, so no cross-dedup),
  recomputing per-policy `action` (`_strongest_match_action`) and `context_words`
  (`_collect_context_words`); preserves policy order.
- Module + method docstrings updated to document the body path.

### ✅ Task 3 — `manual_test/iso_test.py`: keep the oracle and the artifact honest
- `oracle_text` (tabular branch) now includes `td.body`, so the independent verify.py-style oracle
  still sees every value → count parity holds. **(Required — without it the oracle under-counts.)**
- `_render_tabular` renders body matches (which carry `start`/`end`, not col/row) as
  `[body | offset N-M]`.

### ✅ Task 4 — `scripts/harness/test_analyzer_engine.py`: regression tests
- `test_body_far_context_not_boosted` — body value with a context word beyond `context_range` →
  `has_context False`, `allow_log` (the exact bug).
- `test_body_near_context_boosted` — context word adjacent → `block`.
- `test_document_merges_table_and_body` — table cell (header context → block) + body value (no
  context → allow_log) both present in one merged violation; `applied_action == block`.

### Not changed (and why)
- `orchestrator/policy_manager.py` — still calls `engine.analyze_tabular(extract_tabular(...))`,
  which now transparently handles body. No change needed.
- PDF routing — unchanged (decision #2).
- `analyzer/test_cli.py` — abandoned CLI (per the earlier plan, RC7); only prints
  `len(tabular.columns)`, which still works. Left untouched.

## 7. Verification (every command pre-run successfully this session)

Prereq (verified): `D:/Code/GithubPublishEndpointDLP/.venv/Scripts/python.exe` is Python 3.13.14 and
imports `re2, ahocorasick, yaml, lxml, docx, openpyxl, odf, fitz`. On Windows, prefix runs that print
Vietnamese with `PYTHONIOENCODING=utf-8` (cp1252 console otherwise raises `UnicodeEncodeError`).

1. **Unit tests** (run from repo root):
   ```
   PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest scripts/harness/test_analyzer_engine.py scripts/harness/test_events.py -q
   ```
   Result: **19 passed** (16 prior + 3 new).
2. **Full harness suite**:
   ```
   PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest scripts/harness/ -q
   ```
   Result: **131 passed, 3 skipped** (the 3 skips pre-date this change; C#/env-gated).
3. **Corpus run** (writes to a temp dir so your reference `manual_test/iso_test_out` is untouched):
   ```
   PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe manual_test/iso_test.py --corpus "D:/Code/testfile_forDLP/analyzer-test" --out tmp/iso_fixed_out --all
   ```
   Result: **ALL PASS** — every file's analyzer counts == oracle (recall preserved).
4. **Targeted correctness check** — `tmp/iso_fixed_out/docx_lieu_kiem_thu_dlp_v4.extracted.txt`:
   - `0987654321 … context=no`  (was `context=số điện thoại`)
   - `mật … context=no`         (was `context=văn bản`, which had falsely → block)
   - VISA/CCCD with genuine nearby context still `context=<word>` → block.
   And `docx_v3`: table cells still show `sheet=Table 1 | col=Số CCCD | context=CCCD` (header context
   intact) while its body uses `[body | offset …]` proximity. This now matches the PDF twin.

### VM end-to-end (optional, only if you want a full-stack confirmation — NOT required for this fix)
Per `README.md`: build/bundle/deploy to the clean Win11 VM, `Restart-Service`, `dlp-ctl status`, then
copy `docx_v4` to a removable drive / upload via browser → expect **BLOCK** (VISA/CCCD), with the
event log showing the corrected per-policy context words and no `reason=timeout`. This exercises no
new code path beyond `analyze_tabular`, so dev verification is sufficient; the VM step only re-confirms
end-to-end wiring.

## 8. Feasibility / nothing blocked
- No new dependencies, no API/version changes, no deprecated calls — the fix reuses existing
  functions (`analyze`, `normalize_ws`, `_strongest_*`, `_collect_context_words`, dataclasses
  `field`). Pure Python; there is no compile step, and the whole test suite + corpus run green, so
  **no build errors**. No task was blocked or skipped.

## 9. Cleanup
- Throwaway `tmp/probe.py` removed. `tmp/iso_baseline_out` (pre-fix) and `tmp/iso_fixed_out`
  (post-fix) kept under `tmp/` (git-ignored) as before/after evidence; delete at will.
