# Plan — End-user block reasons, supported-format gate, and fail-reason auditing

## Context

Today the DLP agent surfaces block information poorly to the endpoint user and to auditors:

- **Transfer Agent "Note" column** shows a SHA-256 hash for both successful and *policy-blocked* files (the hash carries no meaning for the user), and its only non-hash text is a stale "Analysis timed out" — even though the agent now has many more failure causes. Root cause: the dispatcher hard-codes `reason=""` for the `peripheral_storage` channel (`orchestrator/dispatcher.py:70`), the orchestrator therefore replies just `BLOCK`, and `OrchestratorClient.AnalyzeAsync` never parses a `BLOCK|reason` anyway — so `ErrorMessage` stays null and the Note falls back to `sha256:…`.
- **events.jsonl** records `decision:"BLOCK"` with an empty `violations:[]` on every failure-mode block, so a reader cannot distinguish a timeout from an oversize file from a real policy hit. Industry log schemas (Elastic ECS `event.reason`, OCSF `status_detail`) both carry a dedicated "why" token on blocks — this is a recognised gap.
- **Block text leaks/derives the policy id.** The browser popup reason is built by string-munging the policy `id` (`_format_block_reason`, `orchestrator/dispatcher.py:258`), which is insecure and will read as gibberish if ids become random.
- **The analyzer scans anything.** `extract_text` treats every unrecognised extension as raw UTF-8 text, so a `.exe`/`.jpg`/`.pptx` is "scanned" as garbage rather than refused. There is no explicit supported-format list.

**Intended outcome:** one canonical per-decision *reason* that flows to every user surface (browser popup, clipboard replacement text, Transfer Agent Note) and into the audit log; an admin-editable per-policy end-user message (no policy id shown); friendly per-category messages for analysis failures; and an explicit supported-format allow-list whose misses are handled by the channel's existing `failure_mode`.

## Decisions locked (from Q&A with the user)

1. **Supported formats = the 8 tested + textual fallback.** Allow-list = `.docx .odt .ods .xlsx .csv .txt .md .pdf` **plus** clearly-textual `.tsv .json .yaml .yml .log`. Everything else (`.pptx`, `.odp`, images, archives, unknown/binary) → `unsupported_format` → channel `failure_mode`.
2. **Transfer Agent Note shows:** policy reason on a policy block; a friendly failure reason on analysis failure; the existing copy/skip mechanics notes. **Drop the hash entirely** (no hash on success either → Note blank for a clean transfer; the STATUS column already says TRANSFERRED).
3. **events.jsonl fail-reason = a stable machine category token** (one new field): `policy_violation | oversize | text_cap | unsupported_format | timeout | analysis_error | malformed`.
4. **Failure-mode blocks show per-category friendly messages** (distinct, reveal no internals).
5. **Browser popup** must additionally instruct the user to **refresh the page and abandon the upload** (Google Drive renders the 403 as a network error and otherwise retries).
6. **Clipboard protocol change must be loop-safe** — the service must exclude *its own* clipboard writes from re-analysis, or it loops forever and disables the clipboard.

## The reason taxonomy (central artifact)

One mapping drives every surface. `category` is the machine token logged to events.jsonl; `user message` is the text shown to the user.

| category | trigger | source of user message |
|---|---|---|
| `policy_violation` | a `block` policy matched | the matched policy's new `user_message` field (joined, distinct, if several) |
| `oversize` | file > `limits.max_file_bytes` or text > `clipboard.max_input_bytes` | failure-message table |
| `text_cap` | extracted text > `analyzer.max_extracted_chars` | failure-message table |
| `unsupported_format` | extension not in the allow-list | failure-message table |
| `timeout` | analysis exceeded `service.analysis_timeout_seconds` | failure-message table |
| `analysis_error` | extractor/analyzer raised | failure-message table |
| `malformed` | `kind=file` w/o path, or unknown `kind` | failure-message table |

**Failure-message table (defaults, Vietnamese — admin-editable in code):**
`oversize` → "Tệp vượt quá kích thước cho phép"; `text_cap` → "Tệp quá lớn để quét nội dung"; `unsupported_format` → "Định dạng tệp không được hỗ trợ"; `timeout` → "Quá thời gian quét, vui lòng thử lại"; `analysis_error` → "Không thể quét tệp"; `malformed` → "Yêu cầu không hợp lệ". (Generic policy fallback when a policy has no `user_message`: "Phát hiện dữ liệu nhạy cảm".)

**Where each piece of text lives (and how it is applied):**
- Per-policy `user_message` → `analyzer/policies.yaml` → **hot-reloadable** via `dlp-ctl reload` (rebuilds `DLPEngine`).
- Failure-message table → a new code module `orchestrator/messages.py` → applied at **service restart** (consistent with other orchestrator-side settings).
- `supported_extensions` → `config.yaml` `analyzer.supported_extensions` → applied at **service restart** (the `_reload_callback` re-applies policies + re-broadcasts client sections but does NOT rebuild the orchestrator's own `config` object, so analyzer-side fields like the existing `max_extracted_chars` are start-only; the new key follows the same rule).

## Implementation tasks (in order; each leaves the tree building + tests green)

### Task 1 — Policy `user_message` field + reason constants (analyzer, Python)
- `analyzer/policy.py`: add `user_message: str = ""` to the `Policy` dataclass; in `load_policies`, read `entry.get("user_message", "")`.
- `analyzer/engine.py`: add `user_message: str = ""` to the `Violation` dataclass; populate it from `policy.user_message` at the two `Violation(...)` build sites (text path ~`engine.py:398`, tabular path ~`engine.py:446`) — the policy is already in scope via `self._policy_lookup`.
- New `orchestrator/messages.py`: the failure-message table (`FAILURE_MESSAGES: dict[str,str]`), the `GENERIC_POLICY_MESSAGE`, and a helper `failure_message(category) -> str`. Single home for user-facing failure strings.

### Task 2 — Orchestrator reason plumbing (Python)
- `orchestrator/policy_manager.py`: change `analyze(...)` to return a **3-tuple** `(decision, violations, failure)` where `failure` is a category token or `None`.
  - `_oversize_verdict` → return `(decision, [], "oversize")`; text-cap path → `(decision, [], "text_cap")`; the two hard-BLOCK guards → `(..., [], "malformed")`; all normal action paths → `(decision, violations, None)`.
- `orchestrator/dispatcher.py`:
  - Update the three `future.result()` unpackings to `decision, violations, failure = future.result()`.
  - Map outcome → `(user_reason, category)`: policy block → `category="policy_violation"`, `user_reason=_format_block_reason(violations)`; `failure` set → `category=failure`, `user_reason=messages.failure_message(failure)`; `FutureTimeoutError` → `"timeout"`; `except Exception` → `"analysis_error"`.
  - **Return the `user_reason` for EVERY channel** (delete the `reason=""` hard-codes for clipboard/peripheral at `dispatcher.py:67,70`). `server.py` already wraps `BLOCK|reason`, so clipboard + peripheral now receive the reason.
  - Rewrite `_format_block_reason` to use `getattr(v, "user_message", "")` (distinct, joined with "; "), falling back to `messages.GENERIC_POLICY_MESSAGE` — **no more policy-id munging**.
  - Pass the `category` into the event emit.
- `orchestrator/events.py`: add an optional `reason: str | None = None` kwarg to `record_decision`; include `rec["reason"] = reason` when set. Update the docstring.

### Task 3 — Supported-format gate (Python)
- `orchestrator/config.py`: add `supported_extensions: list` to `OrchestratorConfig` (default = the 13 from decision 1, normalized to lowercase with a leading dot); load from `analyzer.supported_extensions` in `load_config`.
- `orchestrator/policy_manager.py` (file branch, BEFORE extraction): if `os.path.splitext(filename)[1].lower()` not in `self._cfg.supported_extensions` → delete the temp file and `return self._cfg.verdict_for(channel), [], "unsupported_format"` (logged `reason=unsupported_format`). Mirrors the existing oversize early-return.
- `analyzer/extractor.py`: leave the extractor routing intact (it already handles all 13 + plaintext fallback). pptx/odp extractors stay in code but are gated out by default; an admin can re-enable by adding the extension to `supported_extensions`.

### Task 4 — Browser popup (Python, mitmproxy addon)
- `interceptors/browser/addon.py` `_notify_blocked` (~line 1259): append the refresh/abandon guidance to `msg`, e.g.
  `Hành động: Vui lòng TẢI LẠI (refresh) trang web và KHÔNG tải lại tệp này. Tệp bị chặn có thể khiến trình duyệt báo lỗi mạng.`
  Reason text itself already flows via `BLOCK|reason` (`pipe_client.send_and_receive` already parses it) — Task 2 simply makes failure-mode blocks send a *correct* category message instead of today's misleading "Sensitive data detected".

### Task 5 — Transfer Agent Note (C#)
- `interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs`: enlarge the response buffer `256 → 1024`; parse the response — if it starts with `BLOCK`, split on `|` and set `ErrorMessage = reason` (truncate to ~240 chars) on the returned `TransferResult(allowed:false, ...)`. (Connect/analysis-deadline and pipe-failure branches keep their existing local friendly messages.)
- `interceptors/peripheral_storage/TransferAgent/TransferForm.cs` (`ShowDoneStage`, ~line 338): change the Note to `string note = result.ErrorMessage ?? "";` — **drop the hash fallback**. (Leave the SHA-256 computation as-is; it is now unused for display but harmless; removing it is optional cleanup.)

### Task 6 — Clipboard reason + loop-safe self-write (C#)  ⚠ highest-risk
- `src/AgentCore/AnalysisDecision.cs` (or a new file): add `public readonly record struct AnalysisOutcome(AnalysisDecision Decision, string? Reason);`.
- `src/AgentCore/IAgentCore.cs`: change `AnalyseAsync` to return `Task<AnalysisOutcome>`.
- `src/AgentCore/PipeAgentCore.cs`: enlarge the read buffer `16 → 512`; parse `ALLOW` / `BLOCK` / `BLOCK|reason`; return `AnalysisOutcome`. Failure-mode/oversize/pipe-error branches return `new AnalysisOutcome(failVerdict, null)` (a local generic message can be supplied by the service).
- `src/ClipboardInterceptor/ClipboardInterceptorService.cs`:
  - On block, set a **dynamic** replacement text: `$"[DLP] Đã chặn: {reason}"` (or `"[DLP] Đã chặn nội dung"` when reason is null/empty).
  - **Loop-safety:** replace the exact-match self-exclusion at line 40 with a marker-prefix check. Define `private const string DlpMarker = "[DLP";` and gate: `if (content.StartsWith(DlpMarker, StringComparison.Ordinal)) return;`. Both the placeholder `"[DLP: Analyzing...]"` and every dynamic block text start with `"[DLP"`, so all DLP-authored writes are excluded — no re-ingest, no loop. Keep the existing `_allowRestoreText` guard (restored *user* text is not marker-prefixed and still needs it). Extract the guard into a `internal static bool IsDlpAuthored(string content)` so it is unit-testable. Update the doc-comment at the top of the file.
  - Note the residual caveat (a user copy literally beginning with `[DLP` is ignored) — this is the same pre-existing class of risk as today's two-constant guard; acceptable.

### Task 7 — Config, policies, README
- `config.yaml`: under `analyzer:`, add `supported_extensions: [.docx, .odt, .ods, .xlsx, .csv, .tsv, .txt, .md, .pdf, .json, .yaml, .yml, .log]` with a comment (what "supported" means + "applied at service restart, like max_extracted_chars").
- `analyzer/policies.yaml`: add a `user_message:` to each policy (VI): visa → "Phát hiện số thẻ tín dụng (Visa)"; cccd → "Phát hiện số CCCD/CMND"; confidential → "Phát hiện từ khóa tài liệu mật/nội bộ"; phone (allow_log) → "Phát hiện số điện thoại".
- `README.md`: (a) extend the events.jsonl line schema in §A.9 to include `reason`; (b) add the supported-format list + `unsupported_format` behavior near the failure-mode notes (§A.8 step 5 / Appendix `analyzer.*`); (c) note the Transfer Agent Note now shows reasons not a hash, and the browser popup's refresh guidance, in §A.8/§B.3; (d) add `analyzer.supported_extensions` and the per-policy `user_message` to the Appendix config/policy reference.

### Task 8 — Tests
- **Python harness (`scripts/harness/`):**
  - `test_events.py`: bump the three stub PMs (`_StubPM`, `_GatedClipPM`, `_slow`) to return the **3-tuple** `(decision, violations, None)`; update `test_browser_block_event_has_violation_ids` (reason now from `user_message`/generic, and the event line carries `reason:"policy_violation"`); **rewrite** `test_peripheral_block_keeps_empty_client_reason` → `test_peripheral_block_now_sends_reason` (peripheral now returns a non-empty reason and `reason:"policy_violation"` in the event).
  - `test_failure_mode.py`: add assertions that each failure path now tags the right category and friendly message (the harness `pipe_helpers.pipe_send` already returns `(decision, reason)`).
  - New `test_supported_format.py`: a `.exe`/`.bin` file → `unsupported_format` follows `failure_mode` (BLOCK default, ALLOW when fail_open), parallel to the existing oversize/text_cap tests.
  - Add an analyzer engine test that `Violation.user_message` is populated from policy.
- **C# (`src/AgentCore.Tests/`):** update `PipeAgentCoreTests.cs` for the `AnalysisOutcome` return (assert `.Decision` and, for a `BLOCK|reason` stub server, `.Reason`). Add a unit test for `ClipboardInterceptorService.IsDlpAuthored` covering placeholder, dynamic block text, and a normal user string (proves the no-loop guard).

## Files touched (summary)
- Python: `analyzer/policy.py`, `analyzer/engine.py`, `orchestrator/messages.py` (new), `orchestrator/policy_manager.py`, `orchestrator/dispatcher.py`, `orchestrator/events.py`, `orchestrator/config.py`, `interceptors/browser/addon.py`, `config.yaml`, `analyzer/policies.yaml`, `README.md`, `scripts/harness/test_events.py`, `scripts/harness/test_failure_mode.py`, `scripts/harness/test_supported_format.py` (new), an analyzer engine test.
- C#: `src/AgentCore/AnalysisDecision.cs` (+`AnalysisOutcome`), `src/AgentCore/IAgentCore.cs`, `src/AgentCore/PipeAgentCore.cs`, `src/ClipboardInterceptor/ClipboardInterceptorService.cs`, `interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs`, `interceptors/peripheral_storage/TransferAgent/TransferForm.cs`, `src/AgentCore.Tests/PipeAgentCoreTests.cs`, a new ClipboardInterceptor unit test.
- **No new third-party dependencies** (no bundle/embed changes); **no C++ changes** (no MSBuild needed).

## Verification

**Prerequisites:** repo `.venv` activated (per README "Command-form convention"); `dotnet` (10 SDK) on PATH (README A.5 confirms it is). No admin needed for the automated suites (3 admin-pipe tests skip). C++ untouched → no Developer PowerShell required.

**I will pre-run before handing back (these are runnable in this dev session):**
1. `python -m pytest scripts\harness -q` → must remain **`145 passed, 3 skipped`** plus my new test(s) (so e.g. ~147 passed). Grounded in README §A.5.
2. `python -m pytest analyzer -q` (engine/user_message test).
3. `dotnet build src\ClipboardInterceptor\ClipboardInterceptor.csproj -c Debug`, `…\TransferAgent\DlpTransferAgent.csproj -c Debug`, `…\Controller\Controller.csproj -c Debug` → **0 errors** (catches the `AnalysisOutcome`/`IAgentCore` ripple before handoff).
4. `dotnet test src\AgentCore.Tests\AgentCore.Tests.csproj` → all green (was `10 passed`; +the new clipboard-guard test). Grounded in README §A.5.

**Manual / install-time (⚠ NOT PRE-TESTED here — needs the installed agent + a removable drive + browser + a real clipboard; this session has no VM/install). Grounded in README §A.8 & §B.3:**
5. Build + install per README A.4/A.7, then via §A.8: Transfer-block a CCCD-with-context `.docx` → Note shows "Phát hiện số CCCD/CMND" (not a hash); transfer a `.exe` → Note shows "Định dạng tệp không được hỗ trợ".
6. Browser-upload the same file → popup shows the reason **and** the refresh/abandon guidance line.
7. Clipboard: copy card+context text → clipboard becomes `[DLP] Đã chặn: Phát hiện số thẻ tín dụng (Visa)`; **then copy several more times and keep using the clipboard for ~30 s → it must keep working (no freeze/loop)** — the explicit regression check for decision 6. Also re-copy normal text → allowed/restored.
8. `events.jsonl` (`C:\ProgramData\DLP\logs\`): each block line now carries `"reason":"<category>"` (e.g. `policy_violation`, `unsupported_format`, `timeout`).
9. Failure-mode demo from README §A.8 step 5 still BLOCK/ALLOW-flips, and the `dlp-agent.log` line now reads `reason=unsupported_format`/`size_limit`/etc.

**Cannot be done in this session (marked, with reason):** steps 5–9 require an elevated install on a machine with a removable drive, a trusted mitmproxy CA + proxy, and an interactive desktop session — none available here; they mirror the VM checks the README already flags ⚠ NOT PRE-TESTED. Every command I hand over (pytest, the `dotnet build`/`dotnet test` lines) will have been executed successfully here first.

**✅ VM verification PASSED (2026-06-21).** Steps 5–9 were run on the clean Win11 VM and passed, after the post-VM fix below.

## Post-VM-test fix — Gmail strips the file extension (extensionless uploads)

**Symptom (VM):** Gmail uploads of `.txt` / `.csv` / `.md` from the `deny` corpus were all BLOCKed with `reason=unsupported_format` (`dlp-agent.log`: `file=upload ext=(none) -> BLOCK`). `.txt` *always* lost its extension; other plain-text types lost it intermittently. Other channels + the web (Drive) worked.

**Root cause:** the browser addon writes each upload to a temp file named from the filename it could extract; Gmail's upload protocol hands it no real filename, so it falls back to `"upload"` (`addon.py:_write_temp_file`, `:1433`) — no extension. `metadata.filename` is the same `"upload"`. The supported-format gate keyed off the extension and refused anything without one. Peripheral/Transfer Agent was unaffected (it preserves the real extension as `dlp_{guid}{ext}`).

**Fix (orchestrator-side only, `orchestrator/policy_manager.py`):** the gate now refuses only an **explicit, non-empty** extension that is unsupported — an **empty** extension falls through to the analyzer's plaintext path (PII recall preserved; a binary blob with no extension just yields no matches). `.exe`/`.pptx`/images (explicit unsupported ext) still block.

```python
ext = os.path.splitext(filename)[1].lower()
if ext and ext not in self._cfg.supported_extensions:   # was: if ext not in ...
    ... unsupported_format ...
```

- Tests added (`scripts/harness/test_supported_format.py`): extensionless benign file → ALLOW (analyzed, not refused); extensionless card+context file → BLOCK by **policy** (not `unsupported_format`). Full harness now **174 passed, 3 skipped**.
- Docs updated: the `config.yaml` `supported_extensions` comment and README §A.8 note both explain that no-extension files are analyzed (with the Gmail reason).
- Apply on the VM: it's a code change, so update `C:\Program Files\DLP\orchestrator\policy_manager.py` and `Restart-Service DLPAgent` (NOT `dlp-ctl reload`, which doesn't reload orchestrator code).

## Risks / non-goals
- **Clipboard loop** is the dominant risk; mitigated by the marker-prefix guard covering *all* DLP-authored writes + a dedicated unit test + the manual soak in step 7.
- **Response-size:** reasons are capped (~240 chars C# / clipboard buffer 512 B) so the message-mode pipe never truncates mid-reason.
- **Behavior change:** `.pptx`/`.odp` (currently parsed) now block as `unsupported_format` by default — intended per decision 1; re-enable via `supported_extensions`.
- **Non-goals:** no central-server reason push, no per-channel custom message overrides beyond policy `user_message` + the category table, no C++/payload changes.
