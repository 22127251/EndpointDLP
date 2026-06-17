# Analyzer memory/perf hardening + unified per-channel failure handling

**Status: PLAN — not yet implemented (Phase 0 is the only thing already done).**
Separate from `analyzer-fix-and-test-plan.md` and `analyzer-body-context-fix-plan.md`.
Each phase is **independently implementable, verifiable, and markable done**, so work can
stop/resume across sessions. Execution order is the phase numbering below.

---

## Why (context + hard constraints)

- **Target VM: Windows 11 Home, 8 GB RAM, clean.** The orchestrator is ONE process with **3 thread
  pools = 7 concurrent analyses** (clipboard 2 + browser 3 + peripheral 2, `config.yaml pools:`),
  all sharing **one** engine. Concurrency multiplies *transient* memory.
- **The regression:** the body-context fix (Phase 0) routed the docx/odt prose body through
  `engine.analyze()`, which calls `normalize_ws()`. A 6.6 MB docx expands to a **93 MB** body, so
  that became a 5.6 s `re.sub`. docx_b3/odt_b3 went 5.5 s → ~12.4 s (over the 10 s timeout).
- **Memory is the binding constraint (measured, tracemalloc, docx_b3 89 MB body):**
  single-shot normalize ~1.5–1.7 GB; per-paragraph normalize ~0.36 GB; scan copies (lowercase for
  Aho-Corasick + UTF-8 encode for RE2) +1.2 GB ⇒ **one 89 MB-body analysis peaks ~1.5–2 GB**.
  `max_file_bytes` caps **file** size at 100 MB; docx expands ~14× ⇒ ~1.4 GB bodies; a few concurrent
  ⇒ **OOM on 8 GB**.
- **ods/xlsx are extraction-bound** (analyze ~1.2–1.6 s; extract ~4.9–5.0 s via openpyxl/lxml for
  330k–430k cells).
- Real endpoint files are typically **< 5 MB of text**; the 93 MB body is a *synthetic* B3 stress file.

## Decisions (confirmed with the user)
1. Keep full **normalization** on **both** table-column and prose-body paths — done memory-safely via
   **per-unit** normalize (per cell / paragraph / line, never the whole concatenation).
2. **Extracted-text cap, fail-closed** is the primary memory+time bound; the cap value is
   **configurable** and lives in the **analyzer** config section.
3. **python-calamine** (Rust) for xlsx/ods extraction; **hard dependency, no lxml fallback** (if the
   import fails the phase fails loudly — avoids ambiguity about which reader ran). cp313 win_amd64
   wheel confirmed.
4. **Unified per-channel `failure_mode`** = `fail_closed` (→BLOCK, default) | `fail_open` (→ALLOW).
   ONE setting per channel/component covers ALL failure reasons; the specific `reason=` still logged.
5. **browser** default → `fail_closed` (today it fails *open* on pipe error — behavior change, Phase 7).
6. **peripheral_storage** splits into two components, each with its own `failure_mode`:
   `controller` (the DLL injector + C++ `NtCreateFile` hook) and `transfer_agent` (the C# file mover).
7. **Pipe/client-wait timeouts unified to milliseconds.**
8. PDF path unchanged (already correct + fast).

## Target `config.yaml` (final shape; see the sequencing note for *when* each lands)
```yaml
analyzer:
  # Cap on extracted text per analysis (characters; ~2 bytes each in memory). Over it →
  # the channel's failure_mode verdict, logged reason=text_cap. ~16M chars ≈ ~200-400 MB
  # peak/analysis. Configurable.
  max_extracted_chars: 16000000

clipboard:
  max_input_bytes: 1048576     # input text size cap for this interceptor
  failure_mode: fail_closed    # block (fail_closed) | allow (fail_open) on ANY failure
  pipe_timeout_ms: 12000       # client wait for the orchestrator verdict (must exceed analysis budget)

browser:
  max_input_bytes: 104857600   # uploaded-file size cap
  failure_mode: fail_closed    # was fail_behavior:"open" → now blocks uploads if orchestrator is down
  pipe_timeout_ms: 12000       # was pipe_timeout_seconds:12 → ms for consistency with clipboard
  # … domain_blocklist / upload_url_keywords / extensions / mime_types unchanged …

peripheral_storage:
  controller:                  # DLL injector + C++ NtCreateFile hook that blocks removable-drive writes
    failure_mode: fail_closed  # decision INSIDE the hook when shared memory is unavailable/unreadable
    target_processes: [explorer.exe]
    shared_memory_name: UsbDlpDriveMap   # NOT hot-reloadable
    payload_dll_path: Payload.dll        # relative → resolved against Controller.exe dir
    in_user_session: false               # was controller_in_user_session (E0-spike note)
  transfer_agent:              # C# file-transfer UI that sends files to the orchestrator for analysis
    failure_mode: fail_closed  # on pipe/connect/analysis failure; THIS is the verdict the orchestrator
                               # uses for peripheral-channel analysis failures (timeout/size/text_cap)
    connect_timeout_ms: 5000
    analysis_timeout_ms: 12000 # was analysis_timeout_seconds:12 → ms for consistency
```
Removed: `limits.oversize_fail_behavior`, `browser.fail_behavior`, `peripheral_storage.fail_mode`.
The orchestrator's own analysis budget stays **seconds** (`service.analysis_timeout_seconds`) — it is
an internal budget, not a client pipe wait, so it stays in its own unit/section.

### ⚠ Sequencing note (cross-language config coupling)
`config.yaml` is read by the orchestrator (Python) **and** the clients (browser addon = Python,
ClipboardInterceptor + TransferAgent + Controller = C#/C++). Renaming a key a client reads
(`pipe_timeout_*`, the `peripheral_storage` subtree layout, the old `fail_behavior`/`fail_mode`)
breaks that client until it is rebuilt — which needs the C#/C++ build + a VM run. To keep every phase
independently verifiable:
- **Orchestrator-only** config lands in **Phase 1**: add `failure_mode` to `clipboard`/`browser` and a
  nested `peripheral_storage.transfer_agent.failure_mode` (all additive); delete
  `limits.oversize_fail_behavior` (orchestrator-only). The orchestrator consumes these immediately.
- **Client-read** config polish (pipe→ms rename, the `controller`/`transfer_agent` reorg, retiring
  `browser.fail_behavior` + `peripheral_storage.fail_mode`) lands in **Phase 7** atomically with the
  client code changes, so no phase leaves a client reading a renamed key.

---

## Phase 0 — Body-context proximity fix — ✅ DONE (prerequisite)
`TabularData.body` + `analyze_tabular` proximity-merge + tests (`analyzer-body-context-fix-plan.md`).
This is what introduced the normalize-on-body cost that Phase 2 makes cheap. No further action.

## Phase 1 — Orchestrator-side unified `failure_mode` — ✅ DONE
`config.yaml` (deleted `limits.oversize_fail_behavior`; added `failure_mode: fail_closed` to
`clipboard`/`browser` + `peripheral_storage.transfer_agent`), `config.py` (`failure_mode` dict +
`verdict_for(channel)` helper), `policy_manager._oversize_verdict` + `dispatcher` timeout/error paths
now call `verdict_for(channel)` (no hardcoded BLOCK; `reason=` still logged, `failing closed|open`).
Verified: new `scripts/harness/test_failure_mode.py` (oversize/error/timeout × fail_closed/fail_open ×
3 channels) + `conftest.py config_overrides` deep-merge param; `pytest scripts/harness/ -q` =
**137 passed, 3 skipped**. Client-read config polish (pipe→ms, peripheral subtree reorg, retiring
`browser.fail_behavior`/`peripheral_storage.fail_mode`) is still deferred to Phase 7.

## Phase 1 (original detail, retained) — `config.yaml`, `orchestrator/config.py`, `orchestrator/policy_manager.py`, `orchestrator/dispatcher.py`
**Goal (do first — trivial, makes config cleaner):** ONE per-channel setting decides BLOCK/ALLOW for
every *orchestrator-side* failure (timeout, file-size, text-cap, analysis-error). No client-read keys
touched (see sequencing note).
**Steps**
1. `config.yaml`: add `failure_mode: fail_closed` under `clipboard` and `browser`; add
   `peripheral_storage.transfer_agent.failure_mode: fail_closed` (additive nested key); delete
   `limits.oversize_fail_behavior`.
2. `config.py`: parse per-channel `failure_mode` (default `fail_closed`); add helper
   `verdict_for(channel) -> "BLOCK"|"ALLOW"` (peripheral_storage → reads the transfer_agent value).
3. `policy_manager._oversize_verdict`: use `verdict_for(channel)` (keep `reason=file_size` log).
4. `dispatcher`: on `FutureTimeoutError` / analysis `Exception`, return `verdict_for(channel)` instead
   of hardcoded `BLOCK` (keep `reason=timeout` / `reason=error` logs).
**Verify (pre-tested):** new `scripts/harness/test_failure_mode.py` — per channel × {timeout,
oversize, error}, decision follows `failure_mode`; update `test_timeout.py`. `pytest scripts/harness/ -q` green.
**Done when:** every orchestrator-side failure honors the channel `failure_mode`; `reason=` still logged.
**Feasibility: pure Python; reuses existing oversize machinery.**

## Phase 2 — Memory-safe, fast normalization (per-unit) — `analyzer/engine.py`, `analyzer/extractor.py`
**Goal:** never normalize a whole-document string; normalize each small unit. Kills the 1.5–1.7 GB
spike (→ ~0.36 GB) and the time regression, keeps full normalization + identical counts.
**Steps**
1. Keep `normalize_ws` (`" ".join(text.split())`) as the **unit** normalizer (tiny token lists per unit).
2. **Body:** in `analyze_tabular`, `body_text = "\n".join(normalize_ws(p) for p in tabular.body)`; add
   `normalize: bool = True` to `analyze()` and call it with `normalize=False` from `analyze_tabular`.
3. **Table columns:** normalize **per cell** (`col.values = [normalize_ws(v) for v in col.values]`) so
   row-offset recovery stays correct.
4. **Plain path:** when `normalize=True`, collapse **per line** (`"\n".join(normalize_ws(ln) for ln
   in text.split("\n"))`), not one giant `re.sub`.
**Verify:** `pytest scripts/harness/test_analyzer_engine.py -q` green (+ a test that cross-paragraph
proximity still works and counts are unchanged); `iso_test.py --corpus tmp/final-demo/deny --out
tmp/p2_out` ALL PASS; tracemalloc peak on docx_b3 < 0.5 GB.
**Done when:** counts unchanged; docx_b3/odt_b3 ~5–8 s; normalize peak < 0.5 GB.
**Feasibility: validated this session (per-unit = 0.36 GB, counts identical).**

## Phase 3 — calamine xlsx/ods extraction (hard dep, no fallback) — `analyzer/requirements.txt`, `analyzer/extractor.py`
**Goal:** cut ods/xlsx extraction ~5 s → <1 s and shrink its memory.
**Steps**
1. Add `python-calamine` to `requirements.txt` (prebuilt cp313 win_amd64 wheel).
2. Rewrite `_extract_xlsx_tabular` + `_extract_ods_tabular` on calamine
   (`CalamineWorkbook.from_path(...).get_sheet_by_index(i).to_python()` → grid → `_grid_to_columns`),
   `str(...)`-coercing every cell (preserve leading zeros / `+84`). **No lxml fallback** — if calamine
   import fails, fail loudly.
**Verify:** `pytest` green; **gate on parity** — iso_test must still read 80/80/80 for ods_b3/xlsx_b3
(catches number-coercion / leading-zero loss); extraction <1 s.
**Done when:** counts identical AND ods/xlsx extraction <1 s. **Feasibility: wheel + speed confirmed;
value parity to be confirmed by the iso_test gate.**

## Phase 4 — Streaming docx/odt extraction + early-abort hook — `analyzer/extractor.py`
**Goal:** stop building the full lxml tree for huge docs (bounds extraction memory) and provide the
abort hook Phase 5 uses.
**Steps**
1. Convert `_extract_docx_tabular` / `_extract_odt_tabular` from `etree.fromstring(whole)` to
   `etree.iterparse` + `el.clear()` (mirror the existing ODS streamer), accumulating incrementally.
2. Thread a running `char_count`; add a `max_chars` param that raises `ExtractionTooLarge` (new
   exception in `extractor.py`) when exceeded. (Wired by Phase 5.)
**Verify:** `pytest` + iso_test counts unchanged on docx/odt; tracemalloc extraction peak bounded.
**Feasibility: ODS already uses this pattern — low risk.**

## Phase 5 — Extracted-text cap enforced — `config.yaml`, `orchestrator/config.py`, `analyzer/extractor.py`, `orchestrator/policy_manager.py`, `manual_test/iso_test.py`
**Depends on Phase 1 (failure_mode) + Phase 4 (abort hook).**
**Goal:** bound per-analysis memory AND time by refusing to analyze text beyond the cap.
**Steps**
1. `config.yaml analyzer.max_extracted_chars`; `config.py` parses it.
2. Plumb `max_chars` into `extract_text` / `extract_tabular` (+ calamine/streaming readers); raise
   `ExtractionTooLarge` past the cap.
3. `policy_manager.analyze`: `try/except ExtractionTooLarge` → `verdict_for(channel)` with
   `reason=text_cap` (delete temp file, like the oversize path).
4. `iso_test.py`: pass the cap; treat a capped file as a valid BLOCK/ALLOW (per mode), not a parity FAIL.
**Verify:** cap=16M → docx_b3/odt_b3 fail_closed BLOCK (reason=text_cap); B1/B2 + real files unchanged;
tracemalloc peak/analysis < ~0.4 GB; a 7-concurrent sim stays well under 8 GB.
**Done when:** oversized extraction bounded + fails per `failure_mode`; sub-cap files unchanged.
**Feasibility: validated direction; final cap value tuned by measuring peak RSS on the VM.**

## Phase 6 — Single-regex-pass micro-opt — `analyzer/engine.py`
**Goal:** scan the body for all 3 PII policies in **one** pass (one UTF-8 encode + one scan) instead
of three (~−0.35 s on the 93 MB body).
**How it works (the "scanner/lexer" technique):** today each policy is a separate compiled regex, and
`_scan_text` runs `pattern.finditer(text)` **once per policy** — RE2 encodes the whole body to UTF-8
each time, so 3 policies = 3 encodes + 3 scans. Instead, build **one** regex that alternates the
policies as **named groups**:
`(?P<block_visa_all_channels>4\d{3} ?\d{4} ?\d{4} ?\d{4})|(?P<block_cccd_all_channels>\d{12})|(?P<log_phone_numbers>0\d{2} \d{3} \d{4}|\+84\d{9}|0\d{9})`.
A single `finditer` walks the text **once**; for each match exactly one named group is non-empty, and
`m.lastgroup` (or scanning `m.groupdict()`) names the policy that matched → attribute the hit to it.
This is exactly how Python's stdlib `re` tokenizer example and `re.Scanner` work (one master regex of
named alternatives, dispatch on the matched group name). PII matches are few (~240 in docx_b3), so the
per-match group lookup is negligible; the win is doing the expensive encode+scan of the 93 MB body
**once**. Anchoring is unchanged (each alternative keeps its `\b`), and the shapes don't overlap
ambiguously, so first-match-wins alternation is safe.
**Verify:** counts unchanged on the corpus; small time drop. Independent; do near the end.
**Feasibility: RE2 supports named groups; low risk, modest gain.**

## Phase 7 — Client config cleanup + pipe-fail honors `failure_mode` (C#/C++/addon) — interceptors + `config.yaml`
**Largest + riskiest; needs the C# build (`dotnet build`; C++ `.vcxproj` via MSBuild
`C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe`) + a clean-VM
run. Do last; deferrable.**
**Goal:** finish the unified config (the client-read polish from the sequencing note) and make a
**pipe failure** also follow `failure_mode`.
**Steps (with discovery)**
1. **Discover** each client's config loader + pipe-fail path: browser addon (Python mitmproxy),
   `ClipboardInterceptor` (C#), `TransferAgent` (C#), and the peripheral **C++ payload hook**.
2. Apply the client-read config polish: `pipe_timeout_*`→`pipe_timeout_ms`; reorganize
   `peripheral_storage` into `controller:` + `transfer_agent:` subsections; retire `browser.fail_behavior`
   and `peripheral_storage.fail_mode`; `transfer_agent.analysis_timeout_seconds`→`_ms`; clean up/rewrite
   the section comments to say which knob does what.
3. Each client reads its `failure_mode` and applies it on pipe/connect failure; the C++ hook honors
   `controller.failure_mode` on shared-memory-unavailable.
4. **Behavior change to confirm before shipping:** browser default is now `fail_closed` → blocks
   uploads when the orchestrator is down (was fail-open).
**Verify:** build changed C#/C++; on the VM, stop the orchestrator and confirm each channel's pipe-fail
decision matches its `failure_mode`; spot-check the new config layout loads in every client.
**Feasibility: requires C#/C++ changes + VM run; mark blocked items if a client's config plumbing
can't reach `failure_mode` without a larger refactor.**

---

## Expected outcome (after Phases 1–5, cap=16M)
| file | now | after |
|---|---|---|
| docx_b3 / odt_b3 (93 MB body) | 12.3 / 12.4 s, ~1.5–2 GB | **BLOCK reason=text_cap**, bounded mem/time |
| docx_b2 / odt_b2 | 2.7–4.6 s | ~2–3 s, <0.4 GB |
| ods_b3 / xlsx_b3 | 7.7 / 6.8 s | ~3–4 s (calamine), bounded |
| B1 / real files (<5 MB text) | already fast | fully analyzed, counts unchanged |
| 7 concurrent worst case | OOM risk on 8 GB | bounded ≈ 7 × ~0.3 GB ≈ ~2–3 GB |

## Pre-tested commands (reused each phase)
- Tests: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest scripts/harness/ -q`
  (baseline: **131 passed, 3 skipped**; the trailing atexit traceback is a benign Windows pytest
  temp-cleanup quirk).
- Corpus: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe manual_test/iso_test.py --corpus tmp/final-demo/deny --out tmp/<phase>_out`
  (always `--out` a temp dir so `manual_test/iso_test_out` reference output is preserved).
