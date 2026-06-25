# Analyzer memory/perf hardening + unified per-channel failure handling

**Status: Phases 0, 1, 2, 3, 4, 5 DONE. The ~2 GB / 8 GB-VM memory target is MET** — with the 16M-char
cap enforced during extraction, the production-shape 7-worker worst case measures **1.07 GB** (mem_bench
reader; both the all-capped 93 MB-body docx case and the under-cap odt_b1 case). **Phase 6 (single-regex
pass) was implemented then REVERTED — unsafe for an admin-configurable policy set (silent under-detection
on overlapping/ordered admin regex) and negligible benefit (its win was on the now-capped 93 MB body); see
its section.** **Phase 7 = CODE-COMPLETE + dev-box-verified (2026-06-18); VM run pending.** Client config
polish (pipe→ms, `peripheral_storage` split into `controller:`/`transfer_agent:`, retired
`browser.fail_behavior`/`peripheral_storage.fail_mode`) + every client honors `failure_mode` on pipe
failure, PLUS a user-added requirement: the clipboard channel now carries **large inline text** (the
orchestrator data-pipe reassembles MESSAGE-mode fragments — was a single 64 KB read — gated by the new
`clipboard.max_input_bytes`, default 8 MB). Dev-box gates green: pytest **145 passed/3 skipped** (incl.
new `scripts\harness\test_large_clipboard.py`), `dotnet build` (Clipboard/Controller/TransferAgent) +
`dotnet test AgentCore.Tests` **10 passed**, real `config.yaml` loads. README updated (Part C). **Still
needed: the clean-VM §B run** (install, large-clipboard + failure_mode checks, new config layout loads in
every client).
Separate from `analyzer-fix-and-test-plan.md` and `analyzer-body-context-fix-plan.md`.
Each phase is **independently implementable, verifiable, and markable done**, so work can
stop/resume across sessions. (Original execution order was the phase numbering; reprioritized to 4→5
next after the memory measurement showed the cap is the binding lever.)

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

## Phase 2 — Memory-safe, fast normalization (per-unit) — ✅ DONE
`analyzer/engine.py` only (no `extractor.py` change needed — normalization lives in the engine).
`normalize_ws` switched from `re.sub(\s+)` to `" ".join(text.split())` (dropped `import re`/`_WS_RE`);
`analyze(text, channel, normalize=True)` gained the flag and now normalizes **per line** —
`" ".join(norm for line in text.split("\n") if (norm := normalize_ws(line)))`. **Deviation from the
original step 4:** joined the per-line tokens with a **space**, not `"\n"`. The literal
`"\n".join(...)` would preserve embedded newlines and so stop healing PII wrapped across a line break
(breaks `test_normalize_ws_heals_wrapped_pii` + changes counts); `" ".join` is byte-identical to
`" ".join(text.split())` yet never materializes every word at once, so it keeps the memory win **and**
identical counts. Table columns normalize **per cell** (local `values`, offsets recomputed from it);
body normalizes **per paragraph** then calls `analyze(..., normalize=False)`.
Verified: `pytest scripts/harness/ -q` = **138 passed, 3 skipped** (added
`test_body_cross_paragraph_proximity`); `iso_test --corpus tmp/final-demo/deny` = **ALL PASS** (24/24,
analyzer==oracle counts); **docx_b3 12.4 s→6.9 s, odt_b3→6.75 s** (back under the 10 s timeout);
tracemalloc body-normalize peak **2.007 GB→0.376 GB** (< 0.5 GB). Phase 3 (calamine) next.

## Phase 2 (original detail, retained) — `analyzer/engine.py`, `analyzer/extractor.py`
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

## Phase 3 — calamine xlsx/ods extraction (hard dep, no fallback) — ✅ DONE
`analyzer/requirements.txt` + `analyzer/extractor.py`. Added `python-calamine` (0.7.0, prebuilt cp313
win_amd64 wheel) as a HARD dep. Replaced the openpyxl (xlsx) + lxml-iterparse (ods) tabular readers with
one shared `_extract_calamine_tabular(path, max_chars)` (routing: `.xlsx`/`.ods` → it; the old
`_extract_xlsx_tabular`/`_extract_ods_tabular` deleted) + a `_coerce_cell` helper, feeding the existing
`_grid_to_columns`. **Deviation from the original step 2: used `CalamineSheet.iter_rows()` (row stream),
NOT `to_python()`.** `to_python()` materializes the whole grid in Python at once, which would silently
undo the Phase 5 mid-parse cap for ods/xlsx; `iter_rows()` pulls one row at a time so the running
`char_count` can still raise `ExtractionTooLarge` before the full grid lands in Python. `_coerce_cell`
keeps text cells as-is (leading zeros / `+84` preserved — calamine returns stored-text cells as `str`)
and drops an integral float's `.0`; **no fallback** — the `from python_calamine import` raises loudly if
the wheel is missing. (Caveat, documented: calamine parses the sheet mostly eagerly in `from_path`, so
the cap bounds Python-side grid growth, not calamine's internal Rust parse time — fine, the synthetic
100 MB-xlsx worst case then fails closed via the orchestrator analysis timeout, and the 2 GB target was
already met by Phases 4+5.)
Verified: extraction output **byte-identical** (sha256 over cols/headers/values on ods_b1–b3 + xlsx_b1–b3
— `tmp/p3_fingerprint.py`, only timing differs); `pytest scripts/harness/ -q` = **140 passed, 3 skipped**
(unchanged); `iso_test --corpus tmp/final-demo/deny` **no cap = ALL PASS 24/24** (ods_b3/xlsx_b3 == oracle
80/80/80 — number-coercion / leading-zero parity holds) and **--max-chars 16000000 = ALL PASS 24/24**
(ods/xlsx b2+b3 abort mid-parse at ~16.00M chars → block; b1 full parity). **Extraction time (uncapped):
ods 3.0 s→0.76 s (b3), 1.1 s→0.28 s (b2), 0.33 s→0.08 s (b1); xlsx 3.1 s→2.98 s (b3), 1.0 s→0.76 s (b2),
0.51 s→0.22 s (b1)** — every realistic/under-cap file is now well under 1 s; the over-cap b3 synthetics
match the plan's own expected ~3–4 s (and are capped in production). **Extract-only peak RSS (uncapped):
ods_b3 433 MB, xlsx_b3 304 MB** (≤ the lxml docx/odt streamers' 349–351 MB), under-cap b1 64–80 MB — no
memory regression. Phase 6 (single-pass) later tried + REVERTED (unsafe); Phase 7 (client-side, needs VM) remains.

## Phase 3 (original detail, retained) — `analyzer/requirements.txt`, `analyzer/extractor.py`
**Goal:** cut ods/xlsx extraction ~5 s → <1 s and shrink its memory.
**Steps**
1. Add `python-calamine` to `requirements.txt` (prebuilt cp313 win_amd64 wheel).
2. Rewrite `_extract_xlsx_tabular` + `_extract_ods_tabular` on calamine
   (`CalamineWorkbook.from_path(...).get_sheet_by_index(i).to_python()` → grid → `_grid_to_columns`),
   `str(...)`-coercing every cell (preserve leading zeros / `+84`). **No lxml fallback** — if calamine
   import fails, fail loudly. *(Implemented with `iter_rows()` instead of `to_python()` — see DONE note.)*
**Verify:** `pytest` green; **gate on parity** — iso_test must still read 80/80/80 for ods_b3/xlsx_b3
(catches number-coercion / leading-zero loss); extraction <1 s.
**Done when:** counts identical AND ods/xlsx extraction <1 s. **Feasibility: wheel + speed confirmed;
value parity to be confirmed by the iso_test gate.**

## Phase 4 — Streaming docx/odt extraction + early-abort hook — ✅ DONE
`analyzer/extractor.py` only. Added module-level `ExtractionTooLarge(char_count)`. Rewrote
`_extract_docx_tabular` / `_extract_odt_tabular` from `etree.fromstring(whole)` → single-pass
`etree.iterparse(events=("start","end"))` + `el.clear()` (mirrors the ODS streamer). **Body detection
switched from post-hoc `iterancestors` to a `tbl_depth` counter** (incremented on `tbl`/`table` start,
decremented on end): a body `w:p`/`text:p` is collected+cleared only at `tbl_depth==0`; an OUTERMOST
table is processed (then `el.clear()`-ed) at its end event, so cell paragraphs stay intact for table
extraction and never enter the body. `tbl_idx` increments per outermost table (== every table for the
non-nested scope the docstrings already declare). Both functions gained `max_chars: int | None = None`
(default None = no cap; **public `extract_tabular` signature unchanged — Phase 5 plumbs it**); a running
`char_count` over body paragraphs + table cell values raises `ExtractionTooLarge` mid-parse once it
exceeds the cap.
Verified: extraction output **byte-identical** (sha256 fingerprint of cols/headers/values/body on
docx_b1–b3 + odt_b1–b3 — `tmp/p4_fingerprint.py`, only timing differs); `pytest scripts/harness/ -q` =
**138 passed, 3 skipped**; `iso_test --corpus tmp/final-demo/deny` = **ALL PASS 24/24** (analyzer==oracle,
docx/odt 10/30/80, docx_b3 7.1 s / odt_b3 7.1 s, under the 10 s timeout). **Extract-only peak RSS
(mem_bench `--worker --extract-only`): docx_b3 528→349 MB, odt_b3 524→351 MB** (full tree no longer
materialized). Abort hook confirmed: cap=1M aborts at ~1.0004M chars (before the 93M-char body builds);
no-cap / generous-cap path extracts fully. Phase 5 next: wire `analyzer.max_extracted_chars` →
`extract_tabular`/`extract_text` → these readers, and map `ExtractionTooLarge` to `verdict_for(channel)`
with `reason=text_cap` in `policy_manager`.

## Phase 4 (original detail, retained) — `analyzer/extractor.py`
**Goal:** stop building the full lxml tree for huge docs (bounds extraction memory) and provide the
abort hook Phase 5 uses.
**Steps**
1. Convert `_extract_docx_tabular` / `_extract_odt_tabular` from `etree.fromstring(whole)` to
   `etree.iterparse` + `el.clear()` (mirror the existing ODS streamer), accumulating incrementally.
2. Thread a running `char_count`; add a `max_chars` param that raises `ExtractionTooLarge` (new
   exception in `extractor.py`) when exceeded. (Wired by Phase 5.)
**Verify:** `pytest` + iso_test counts unchanged on docx/odt; tracemalloc extraction peak bounded.
**Feasibility: ODS already uses this pattern — low risk.**

## Phase 5 — Extracted-text cap enforced — ✅ DONE
`config.yaml` + `orchestrator/config.py` + `analyzer/extractor.py` + `orchestrator/policy_manager.py` +
`manual_test/iso_test.py` + `scripts/harness/test_failure_mode.py`.
- **config.yaml:** new top-level `analyzer:` section with `max_extracted_chars: 16000000` (commented:
  enforced DURING extraction because max_file_bytes can't bound a 14×-expanding docx body; `<=0` disables).
- **config.py:** parses `analyzer.max_extracted_chars` (default 16_000_000) into a new
  `OrchestratorConfig.max_extracted_chars` field.
- **extractor.py:** `extract_text(path, max_chars=None)` and `extract_tabular(path, max_chars=None)` thread
  the cap. The four zip-EXPANDING readers enforce it **mid-parse** (raise `ExtractionTooLarge` the instant
  the running char_count crosses the cap, before the full body materializes): docx/odt (Phase 4),
  **ods** (added: per repeat-expanded row) and **xlsx** (added: switched `list(iter_rows())` → a streaming
  row loop so openpyxl aborts before the whole sheet is read). csv/pdf (bounded by max_file_bytes, no zip
  expansion) get a post-hoc total via `_enforce_tabular_cap`; `extract_text` does a post-hoc `len()` check.
- **policy_manager.analyze (file branch):** passes `max_chars` (cfg value, `<=0`→None), and
  `except ExtractionTooLarge` → `verdict_for(channel)` logged `reason=text_cap` (the existing `finally`
  still deletes the temp file).
- **iso_test.py:** `--max-chars` (default None = full-parity regression gate); a capped file is recorded
  as mode=`capped`, verdict block, a valid PASS (deny corpus), parity skipped.
**Verify (all green):** cap=16M refuses docx/ods/odt/xlsx **b2 AND b3** mid-parse at ~16.00M chars
(**correction to the original verify line: b2 ≈ 20–35M chars also exceeds 16M, so only the b1 set +
csv/md/pdf/txt stay under** — confirmed by char-count probe). `pytest scripts/harness/ -q` =
**140 passed, 3 skipped** (+2 `test_text_cap_follows_failure_mode` cases: fail_closed→BLOCK,
fail_open→ALLOW). `iso_test` **no cap = ALL PASS 24/24** (full parity, no regression); **--max-chars
16000000 = ALL PASS 24/24** (8 capped→block, 16 full-parity) and the cap also slashes time on the capped
files (docx_b3 7.1 s→0.7 s, ods_b3 8.9 s→1.4 s). **Memory (mem_bench reader, ONE engine + 7 threads):
7×docx_b3 (all abort at 16M) and 7×odt_b1 (all fully analyzed) both peak 1.07 GB** ⇒ ≤2 GB target met.
Phase 7 next (client-side; needs VM). (Phase 3 since done; Phase 6 since tried + REVERTED — unsafe.)

## Phase 5 (original detail, retained) — `config.yaml`, `orchestrator/config.py`, `analyzer/extractor.py`, `orchestrator/policy_manager.py`, `manual_test/iso_test.py`
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

## Phase 6 — Single-regex-pass micro-opt — ❌ WON'T DO (implemented, then REVERTED 2026-06-18)
**Reverted: unsafe for an admin-configurable policy set, and the benefit is negligible. Do NOT re-attempt
without changing the design.** It was built (master alternation of named groups `(?P<g0>…)|(?P<g1>…)`,
single `finditer`, `m.lastgroup`→policy) and passed `pytest` 140/3 + `iso_test` 24/24 on the *current* 3
policies — but those gates only prove it works for today's hand-tuned, mutually-disjoint shapes, NOT for
arbitrary admin policies.
**Why it's unsafe (verified):** the old engine runs **one independent `finditer` per policy**, so every
policy reports *all* its matches regardless of the others. One master alternation instead makes policies
**compete for spans** (`finditer` returns non-overlapping matches across the whole alternation and skips
past each), which equals the per-policy union **only if no two policies ever match overlapping text** —
not a property the admin is constrained to keep. Concretely, with admin-authored regex:
1. **Overlapping policies silently drop matches** — `cat` + `category` on "category" → only `cat` (the
   `category` policy never fires). For a DLP control this is **under-detection = data leaks**, with no error.
2. **Same start, different length → only the first-listed policy fires** — RE2 is leftmost-**first**
   (Perl), not longest: `\d{4}` before `\d{4}-\d{4}` on "1234-5678" never fires the longer policy.
3. **Declaration order in the YAML now changes detections** — reordering policies silently alters results.
4. **Group-name collision is silent** — an admin pattern containing its own `(?P<g0>…)` is NOT rejected by
   RE2 (duplicate names tolerated); dispatch becomes ambiguous/version-dependent → wrong-policy attribution.
5. **Blast radius** — all policies compile into one regex: a single bad/oversized admin pattern fails the
   whole master compile (all regex detection dies), and a large policy set can exceed RE2's program-size
   budget that per-policy compiles stay under.
"Proving the policies are mutually non-overlapping" is undecidable in general, so there's no reliable
load-time guard without restricting admin expressiveness.
**Benefit was negligible anyway:** the measured −0.68 s was on the 93 MB docx body, which **Phase 5 caps at
16 M** (≈ −0.12 s realized); the per-column tabular gain is small. Not worth a silent under-detection risk
in a security control. (The denylist/Aho-Corasick path was never involved.) Only Phase 7 (client-side,
needs VM) remains.

## Phase 6 (original detail, retained — ⚠ DO NOT IMPLEMENT, see the WON'T DO note above) — `analyzer/engine.py`
**Goal:** scan the body for all 3 PII policies in **one** pass (one UTF-8 encode + one scan) instead
of three (~−0.35 s on the 93 MB body). *(This design is unsafe for admin-configurable policies — overlapping
or order-dependent admin regex silently under-detect. Retained only for context.)*
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
**✅ CODE-COMPLETE + dev-box-verified 2026-06-18; clean-VM §B run still pending.** Implemented:
`config.yaml` per-component rename (clipboard `max_input_bytes`/`pipe_timeout_ms`/`failure_mode`; browser
`pipe_timeout_ms`/`failure_mode`, retired `fail_behavior`; `peripheral_storage` split into `controller:`
[`failure_mode` was `fail_mode` open/closed, `in_user_session` was `controller_in_user_session`] +
`transfer_agent:` [`analysis_timeout_ms` was `_seconds`, +`failure_mode`]). Clients honor `failure_mode`
on pipe failure: `PipeAgentCore` (provider tuple gained `MaxContentBytes`+`FailOpen`; literal ctor keeps
1 MB/fail-closed for tests), browser `addon.py`/`config.py`, `TransferAgent/OrchestratorClient.cs`,
`Controller` (`AppConfig`+`PeripheralStorageSection` wrapper; ctl-pipe plucks `peripheral_storage.controller`).
Orchestrator: `config.py` sources `max_clipboard_bytes` from `clipboard.max_input_bytes` (back-compat to
`limits.max_clipboard_bytes`); `supervisor.py` reads `controller.in_user_session` (back-compat). **C++
Payload/ShellExtension UNCHANGED** (the hook reads the fail-closed bit from shared memory the Controller
writes; only the Controller's mapping changed) — so no MSBuild step this phase. **User-added large-clipboard
support:** `server.py:_read_message` reassembles MESSAGE-mode fragments (loop on `ERROR_MORE_DATA`,
spike-confirmed pywin32 returns it without raising) bounded by an abuse ceiling; ClipboardInterceptor reads
`clipboard.max_input_bytes` (default 8 MB) + hot-reloads it. **Behavior changes flagged:** browser default
`fail_open`→`fail_closed` (blocks uploads when orchestrator down); controller `open`→`fail_closed`
(blocks removable WRITE/dir-create only, only when shared memory is unavailable). Dev-box gates: pytest
**145/3** (+`test_large_clipboard.py`), `dotnet test AgentCore.Tests` **10**, 3 C# apps build, real
`config.yaml` loads.
(Original step detail below; kept for the VM checklist.) **Needs the C# build (`dotnet build`) + a clean-VM
run for final sign-off.**
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
- **Memory (the real constraint — use this, NOT tracemalloc): `manual_test/mem_bench.py`.**
  Reports Windows **PeakWorkingSetSize** (matches Task Manager). `tracemalloc` only sees CPython-heap
  allocs and is blind to re2's UTF-8 encode + `str.lower()` scan copies and lxml's C tree, so it
  drastically under-reports (it said normalize=0.38 GB; real per-file RSS is ~1.1 GB). Commands:
  per-file isolated `mem_bench.py --corpus tmp/final-demo/deny`; whole-corpus one process (mirrors
  iso_test / Task Manager) `--single-process`; leak check `--file <f> --repeat 3` (flat `rss_trend`).
  **Measured (post-Phase-2), per-file isolated:** docx_b3 1121 MB, odt_b3 1132 MB; single-process
  whole-corpus peak 1.13 GB. But that naive "7 × per-file" overcounts.
  **Production-shape concurrency (`--concurrent N --file X`: ONE shared engine, N threads — what the
  orchestrator's 7-way pools actually do) is the real budget, and transients do NOT fully stack**
  (extraction trees + per-column scan buffers free fast): **7×10M-char docx = 0.64 GB**, 5×10M = 0.53 GB,
  **7×34.5M-char docx (uncapped) = 2.29 GB** (and 11.5 s → also times out). Linear in chars: 7 concurrent
  stays ≤ 2 GB up to ~30M chars/file. **full peak (MB) ≈ 50 + ~12·(Mchars)** per file.
  ⇒ **Answer to "can the agent worst case be ≤2 GB?": yes — with the extracted-text cap enforced
  DURING extraction.** A 16M-char cap puts the 7-worker worst case at **~1.0 GB**; real endpoint files
  (<5 MB text ≈ 5M chars) → 7 concurrent ≈ 0.4 GB. **The cap MUST fire while streaming (Phase 4), not
  after**: `max_file_bytes`=100 MB lets a 6.6 MB docx→93M chars, so a 100 MB docx → ~1.4 BILLION-char
  body → OOM in extraction before any post-hoc cap check. So **Phases 4 + 5 are the levers; Phase 3
  (calamine) only trims further and is not needed to hit 2 GB** (Phase 6 single-pass was tried + REVERTED —
  unsafe for admin policies, and its win was on the now-capped body anyway).
