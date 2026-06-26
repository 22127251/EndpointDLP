# Plan — Config hot-reload, English notifications, and clipboard text-cap removal

> **Status: IMPLEMENTED + fully verified (2026-06-24).** Dev box: Python harness
> **222 passed, 3 skipped** (was 145/3 — +77 from the new config-reload layers); C#
> **AgentCore.Tests 17 passed**; ClipboardInterceptor + TransferAgent build clean. **Clean
> Windows 11 VM: the full build → bundle → install → end-to-end decision tests → uninstall
> run PASSED** — including the headline live checks: English block text everywhere, the
> clipboard large-copy + `analyzer.max_extracted_chars` `text_cap` reload, and the
> server-side `failure_mode` BLOCK↔ALLOW flip on `dlp-ctl reload` (`config hot-reload
> applied: …` in `dlp-agent.log`). README §A.2/A.3/A.6/A.7/A.8/A.9/A.10 and all of §B are
> now tagged **✅ MANUALLY TESTED** (A.4's Developer-PowerShell C++ build is the only
> remaining ⚠️). Nothing left to verify.
> **Author's answers already incorporated:** (1) translate the shipped policy
> `user_message` block reasons to English; (2) over-cap clipboard text **follows
> `failure_mode`** (reason `text_cap`); (3) make **both** `supported_extensions`
> **and** `analysis_timeout_seconds` hot-reloadable.

---

## 0. Glossary (terms used throughout — defined once)

- **ctl-pipe** — the duplex named pipe (`ctl_pipe` in `config.yaml`) over which the
  orchestrator *pushes* per-component config to the long-lived interceptor clients
  (`clipboard`, `browser`, `controller`). Implemented by `orchestrator/ctl_server.py`
  (server) + `CtlPipeSubscriber` (C# in `DlpShared`, Python in
  `interceptors/browser/ctl_pipe_subscriber.py`).
- **`OrchestratorConfig`** — the parsed-config dataclass in `orchestrator/config.py`.
  One instance is built at startup and **shared by reference** by `PolicyManager`,
  `Dispatcher`, and `PipeServer`.
- **failure_mode** — the per-channel verdict used when analysis cannot complete
  (`fail_closed` → BLOCK, the default; `fail_open` → ALLOW). Lives on each channel's
  config section. Server-side it is read through `OrchestratorConfig.verdict_for(channel)`.
- **text_cap** — the failure category (logged to `events.jsonl` as `reason=text_cap`)
  used when extracted/scanned text exceeds `analyzer.max_extracted_chars`.
- **oversize** — the failure category for a *file* larger than `limits.max_file_bytes`.
- **hot-reloadable** — a setting that takes effect on `dlp-ctl reload` **or** on saving
  `config.yaml` (the file-watcher auto-applies), with **no service restart**.

---

## 1. Findings — what is actually wired today (the "why")

### 1.1 Config reload only reaches the *clients* and App Control, never the orchestrator's own logic

`orchestrator/__main__.py::_handle_config_change` (the single bridge invoked by both
the `config.yaml` file-watcher and `dlp-ctl reload`) does exactly three things on a
config change:

1. overrides the three pipe names back to their in-use values (they are restart-only),
2. updates `raw_cell["raw"]` and calls `ctl_server.broadcast()` — which **pushes the
   per-component sections to the clients** (clipboard/browser/controller), and
3. calls `app_control_channel.apply_config(new_raw)`.

It **never** updates the live `OrchestratorConfig` object. But `PolicyManager`,
`Dispatcher`, and `PipeServer` read all their server-side knobs from that frozen
object (or from values cached at construction). **Consequence:** every orchestrator-side
setting is effectively *restart-only today*, even though the README/`config.yaml`
comments imply some of them reload. Specifically these are silently frozen after start:

| Setting | Read at runtime by | Reloads today? |
|---|---|---|
| `clipboard/browser/peripheral_storage.transfer_agent.failure_mode` (server-side verdict on a failure) | `Dispatcher` + `PolicyManager` via `verdict_for()` | **No** (frozen) |
| `limits.max_file_bytes` | `PolicyManager.analyze` (file branch) | **No** |
| `analyzer.max_extracted_chars` | `PolicyManager.analyze` (extraction cap) | **No** |
| `analyzer.supported_extensions` | `PolicyManager.analyze` (format gate) | **No** (docs already say "restart") |
| `service.analysis_timeout_seconds` | `Dispatcher` (`future.result(timeout=…)`) | **No** (cached in `__init__`) |
| `service.drain_timeout_seconds` | `run_core` shutdown | **No** |

> The client-side knobs (`clipboard.pipe_timeout_ms`/`failure_mode`,
> `browser.pipe_timeout_ms`/`failure_mode`, `peripheral_storage.controller.*`)
> **do** hot-reload correctly today — they ride the ctl-pipe broadcast and each client
> applies them (`ClipboardInterceptor/Program.cs`, `addon._apply_ctl_update`,
> `Controller/Program.cs::TryReload`). The TransferAgent re-reads `config.yaml` on every
> launch (it is short-lived and intentionally not a ctl subscriber), so its section is
> always "fresh". Those paths are **left unchanged**.

> **Why this matters for Task 3:** today the clipboard oversize demo "works" only
> because the *client* enforces the byte cap before sending. Once we remove that client
> cap (Task 3), the oversize→`failure_mode` decision moves entirely server-side — which
> is exactly the frozen path above. So **Task A (server hot-reload) is a prerequisite
> for Task B's `failure_mode` behavior to be reloadable at all.**

### 1.2 Notifications still contain Vietnamese

End-user-facing strings still in Vietnamese (verified by a diacritic sweep over all
non-test, non-vendored source):

| File | String |
|---|---|
| `orchestrator/messages.py` | `GENERIC_POLICY_MESSAGE` + all 6 `FAILURE_MESSAGES` + `_UNKNOWN_FAILURE_MESSAGE` |
| `src/ClipboardInterceptor/ClipboardInterceptorService.cs` | `BlockFallback = "[DLP] Đã chặn nội dung"`, `BuildBlockText` → `"[DLP] Đã chặn: {reason}"` |
| `interceptors/browser/addon.py` | `_notify_blocked` → the `guidance` paragraph |
| `interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs` | line ~134 fallback `"Tệp bị chặn bởi chính sách bảo mật."` |
| `analyzer/policies.yaml` | the 4 `user_message:` values |

The TransferAgent **window** (`TransferForm.cs`) and the browser popup **title/labels**
are already English.

> **Detection content stays Vietnamese on purpose.** `context_words`/`keywords` in
> `policies.yaml` (e.g. `thẻ`, `CCCD`, `nội bộ`) are *matchers* against Vietnamese PII —
> they are NOT notifications, so they are not touched. Only the `user_message` (the
> reason shown to the user) is translated.

### 1.3 The clipboard text cap is enforced in three places

The cap is `clipboard.max_input_bytes` (default 8 MB), surfaced server-side as
`OrchestratorConfig.max_clipboard_bytes` (back-compat alias `limits.max_clipboard_bytes`):

1. **Client, pre-send** — `PipeAgentCore.AnalyseAsync` rejects
   `UTF8.GetByteCount(content) > maxContentBytes` before opening the pipe. This is the
   "text cap implementation of the clipboard interceptor" to remove.
2. **Server, pre-analyze** — `PolicyManager.analyze` (kind=`text`) returns the oversize
   verdict when `bytes > max_clipboard_bytes`.
3. **Server, pipe-read memory ceiling** — `server.py::_read_message` bounds the
   reassembled message at `max(max_clipboard_bytes, _BUFFER)*2 + 1 MB`.

Windows imposes **no** hard clipboard-text size limit (memory-bound only — confirmed via
docs.microsoft.com/oldnewthing), so #3 must remain *some* memory ceiling even after the
user-facing cap is gone; we re-derive it from `max_extracted_chars` (§4.3).

For `kind=text`, `max_extracted_chars` is **not** applied today — that cap currently only
runs during *file* extraction. Task B makes the clipboard text path honor it.

### 1.4 Existing test coverage — and the exact gap

- `scripts/harness/test_hot_reload.py` covers **policy** hot-reload only (swap
  `policies.yaml`, confirm new BLOCK behavior). Nothing about `config.yaml`.
- `scripts/harness/test_ctl_pipe.py` covers the **ctl-pipe broadcast payload**: a
  snapshot matches disk, and on a single save it asserts `data_pipe` is overridden back
  to the in-use value while **two** client fields (`browser.failure_mode`,
  `clipboard.pipe_timeout_ms`) propagate. This proves only the **orchestrator's outbound
  broadcast**, not (a) the orchestrator's own consumption of any field, nor (b) the full
  set of client fields, nor (c) that the supposedly non-reloadable fields actually stay
  frozen end-to-end.
- **Gap:** there is **no** test that the live orchestrator picks up reloaded server-side
  knobs, and **no** test that the restart-only fields are inert on reload. §7.3 adds an
  exhaustive, field-by-field test layer for both directions (your explicit ask).

---

## 2. Recommended hot-reload classification (your "suggest me what is / isn't")

**Hot-reloadable (apply on `dlp-ctl reload` or `config.yaml` save, no restart):**

| Setting | Where consumed | Notes |
|---|---|---|
| `clipboard/browser/transfer_agent.failure_mode` | client **and** server | client already reloads; **server made to reload** (Task A) |
| `clipboard.pipe_timeout_ms`, `browser.pipe_timeout_ms` | clients | already reload |
| `peripheral_storage.controller.{failure_mode,target_processes,payload_dll_path}` | Controller | already reload (`shared_memory_name` rejected at runtime by design) |
| `limits.max_file_bytes` | server | **made to reload** (Task A) |
| `analyzer.max_extracted_chars` | server | **made to reload** (Task A) — now also governs clipboard text (Task B) |
| `analyzer.supported_extensions` | server | **made to reload** (Task A) — per your answer |
| `service.analysis_timeout_seconds` | server (`Dispatcher`) | **made to reload** (Task A) — per your answer; see invariant note |
| `service.drain_timeout_seconds` | server (shutdown) | **made to reload** (Task A); only observed at next stop |
| `app_control.{poll_seconds,forward_block_events}` | App Control channel | already reload via `apply_config` |
| all policy rules in `analyzer/policies.yaml` (incl. `user_message`) | analyzer | already reload via `PolicyManager`'s own watcher |

**NOT hot-reloadable (require a service restart — and the agent will log a "requires
restart" warning if changed, as it already does for the pipe names):**

| Setting | Why restart-only |
|---|---|
| `data_pipe`, `ctl_pipe`, `admin_pipe` | pipe handles are bound at startup (already handled + warned) |
| `pools.{clipboard,browser,peripheral_storage}_workers`, `pools.pipe_listeners` | the `ThreadPoolExecutor`s and accept threads are sized once at construction |
| `paths.*` (exe/dll/addon/log_dir) | consumed by the supervisor when it spawns children and by the installer |
| `proxy.{listen_port,bypass}` | bound by the spawned `mitmdump` child |
| `policies_file` | the *path* is resolved at `PolicyManager` construction (policy **content** still hot-reloads) |
| `supervisor.{max_restarts,restart_window_seconds,stable_uptime_reset_seconds}` | read by `Supervisor` at construction |
| `install.*` | install-time only |
| `app_control.{enabled,inbox_dir,rejected_dir,staging_dir,extra_paths}` | starting/stopping the channel or moving its dirs needs a restart |

> **Invariant to preserve for `analysis_timeout_seconds`:** every client pipe timeout
> must exceed the server analysis timeout (config ships analysis = 10 s, clients wait
> 12 s). Both sides are hot-reloadable, so an admin who lowers `analysis_timeout_seconds`
> is safe; an admin who *raises* it past a client's `pipe_timeout_ms` would make that
> client give up early. The plan does **not** auto-enforce this (it never has) but the
> README note (§7) will call it out, mirroring the existing comment in `config.yaml`.

---

## 3. Task A — Make the orchestrator consume hot-reloadable config on reload

**Goal:** when `config.yaml` changes (file save) or `dlp-ctl reload` runs, the live
orchestrator picks up the hot-reloadable fields from §2 **without a restart**, while
restart-only fields are ignored (with a one-line warning if they changed).

### A.1 `orchestrator/config.py`

1. **Refactor** the body of `load_config` into a pure `_config_from_raw(raw: dict) ->
   OrchestratorConfig` helper; `load_config(path)` becomes "open file → `yaml.safe_load`
   → `_config_from_raw(raw)`". No behavior change — this just lets reload reuse the exact
   same parsing (failure_mode dict, `_normalize_extensions`, defaults).
2. Add a class attribute listing the hot-reloadable field names and a method:

   ```python
   _HOT_RELOADABLE_FIELDS = (
       "failure_mode", "max_file_bytes", "max_extracted_chars",
       "supported_extensions", "analysis_timeout_seconds", "drain_timeout_seconds",
   )

   def apply_hot_reload(self, new_raw: dict) -> list[str]:
       """Recompute the hot-reloadable fields from new_raw and assign them in
       place (every consumer holds THIS object by reference, so the swap is what
       makes a reload take effect). Restart-only fields are left untouched.
       Returns the names of the fields whose value actually changed (for logging).
       Each assignment is a single attribute/reference set → atomic under the GIL,
       so worker threads reading concurrently see either the old or new value,
       never a torn one (no extra lock needed)."""
       fresh = _config_from_raw(new_raw)
       changed = []
       for name in self._HOT_RELOADABLE_FIELDS:
           if getattr(self, name) != getattr(fresh, name):
               setattr(self, name, getattr(fresh, name))
               changed.append(name)
       self.raw = new_raw
       return changed
   ```

   > `max_clipboard_bytes` is intentionally **absent** from this list — it is removed in
   > Task B. After Task B the field no longer exists.

### A.2 `orchestrator/__main__.py::_handle_config_change`

After the existing pipe-name override block, insert:

```python
changed = config.apply_hot_reload(new_raw)
if changed:
    log.info("config hot-reload applied: %s", ", ".join(sorted(changed)))
```

(Place it before `raw_cell["raw"] = new_raw` / `ctl_server.broadcast()` so the
orchestrator and the clients update from the same snapshot.) The existing pipe-name
"requires restart" warnings already cover the restart-only pipe fields; no other
restart-only field needs a warning because none of them are even read after start, but
we will add a short comment pointing maintainers to §2.

### A.3 `orchestrator/dispatcher.py`

`analysis_timeout_seconds` is cached in `__init__` as `self._analysis_timeout`. Convert
it to a **read-through property** so a hot-reload is observed on the next analysis:

```python
@property
def _analysis_timeout(self) -> float:
    return self._cfg.analysis_timeout_seconds
```

Delete the `self._analysis_timeout = getattr(cfg, …)` line in `__init__`. All three
existing `future.result(timeout=self._analysis_timeout)` sites and the log lines keep
working unchanged (they now read live). `verdict_for()` already reads `self._cfg` live,
so `failure_mode` needs no dispatcher change.

### A.4 `PolicyManager` / `PipeServer`

No structural change: both already read `self._cfg.<field>` / `getattr(self._cfg, …)`
at call time, so mutating the shared `OrchestratorConfig` in A.1 is observed live. (The
`policies_file` path stays frozen — restart-only by design; policy *content* reloads via
the existing `PolicyManager` watcher.)

---

## 4. Task B — Remove the clipboard text cap; let `max_extracted_chars` govern

**Goal:** the ClipboardInterceptor sends whatever the Windows clipboard yields (no
client byte cap); the analyzer decides whether to scan based on
`analyzer.max_extracted_chars`. Over-cap text is **not scanned** and follows
`clipboard.failure_mode` (reason `text_cap`).

### 4.1 Client (C#) — delete the cap plumbing

- **`src/AgentCore/PipeAgentCore.cs`**
  - Change the provider tuple from `(string PipeName, int TimeoutMs, int
    MaxContentBytes, bool FailOpen)` → `(string PipeName, int TimeoutMs, bool FailOpen)`
    (provider ctor + the `_provider?.Invoke() ?? (...)` fallback).
  - Remove `DefaultMaxContentBytes`, `_constantMaxContentBytes`, and the
    `if (Encoding.UTF8.GetByteCount(content) > maxContentBytes) return failVerdict;`
    guard. The literal-value ctor `(string pipeName, int timeoutMs)` signature is
    **unchanged** (so `PipeAgentCoreTests` keep compiling — they only use that ctor).
- **`src/ClipboardInterceptor/ClipboardConfigHolder.cs`**
  - Drop `_maxInputBytes`, `MaxInputBytes`, `SetMaxInputBytes`, and the
    `MaxContentBytes` element of the `Get()` tuple → `(PipeName, TimeoutMs, FailOpen)`.
  - Remove `ClipboardSection.MaxInputBytes`.
- **`src/ClipboardInterceptor/Program.cs`**
  - Drop `clipboardCfg.MaxInputBytes` from the holder ctor + the startup log line.
  - Delete the `clip.TryGetProperty("max_input_bytes", …)` block in the ctl callback.

### 4.2 Server — `orchestrator/policy_manager.py`

In the `kind == "text"` branch, replace the `max_clipboard_bytes` oversize check with a
`max_extracted_chars` cap that mirrors the file `text_cap` path:

```python
if kind == "text":
    body = text or ""
    cap_chars = self._cfg.max_extracted_chars
    if cap_chars and cap_chars > 0 and len(body) > cap_chars:
        decision = self._cfg.verdict_for(channel)
        log.warning("reason=text_cap req=%s channel=%s text chars=%d > cap=%d -> %s",
                    req_id, channel, len(body), cap_chars, decision)
        return decision, [], "text_cap"
    result = engine.analyze(body, channel)
    ...
```

`_oversize_verdict` and the `oversize` category remain (still used by the **file**
size-cap path). The clipboard text path now emits `text_cap`, which already maps to a
user message and an `events.jsonl` reason.

### 4.3 Server — `orchestrator/server.py::_read_message` memory ceiling

Replace the `max_clipboard_bytes`-derived ceiling with one derived from
`max_extracted_chars`, exposed as a small helper on `OrchestratorConfig` so it tracks
hot-reloads:

```python
# config.py
_CLIPBOARD_UNCAPPED_CEILING_BYTES = 256 * 1024 * 1024  # hard safety bound when cap disabled

def clipboard_pipe_ceiling_bytes(self) -> int:
    """Max bytes the data-pipe will reassemble for one (clipboard) message.
    Derived from max_extracted_chars: UTF-8 is <=4 bytes/char, + 1 MB envelope/
    escaping headroom. With the char-cap disabled (<=0), fall back to a fixed
    256 MB safety bound (the clipboard is otherwise memory-bound only)."""
    cap = self.max_extracted_chars
    return cap * 4 + (1 << 20) if cap and cap > 0 else _CLIPBOARD_UNCAPPED_CEILING_BYTES
```

```python
# server.py::_read_message
ceiling = max(self._config.clipboard_pipe_ceiling_bytes(), _BUFFER)
```

> **Behavior at the edges (documented, fail-safe):** text whose char count ≤
> `max_extracted_chars` always fits and is scanned. Text over the cap (but whose raw
> JSON is within the ceiling) is received and cleanly refused with `text_cap` →
> `failure_mode`. Pathologically huge text (raw JSON beyond the ceiling, e.g. >~64 MB at
> the default cap, or >256 MB when the cap is disabled) is dropped at the pipe and the
> client fails per its own `failure_mode` — still BLOCK by default. This keeps per-analysis
> memory bounded on the 8 GB VM.

### 4.4 Remove `max_clipboard_bytes` from `OrchestratorConfig` + `config.yaml`

- `config.py`: delete the `max_clipboard_bytes` dataclass field and its sourcing
  (`clipboard_cfg.get("max_input_bytes", limits.get("max_clipboard_bytes", …))`).
- `config.yaml`: delete `clipboard.max_input_bytes` and the stale comment block in
  `limits:` that references it; update the `clipboard:` and `analyzer.max_extracted_chars`
  comments to state that clipboard text is now governed by `max_extracted_chars`.

---

## 5. Task C — English notifications (all channels)

Exact string replacements (final wording — adjust if you prefer different phrasing):

**`orchestrator/messages.py`** (also update the module docstring: default language is now
English):
- `GENERIC_POLICY_MESSAGE` → `"Sensitive data detected"`
- `FAILURE_MESSAGES`: `oversize` → `"File exceeds the maximum allowed size"`;
  `text_cap` → `"Content is too large to scan"`;
  `unsupported_format` → `"File type is not supported"`;
  `timeout` → `"Scan timed out, please try again"`;
  `analysis_error` → `"Unable to scan the content"`;
  `malformed` → `"Invalid request"`
- `_UNKNOWN_FAILURE_MESSAGE` → `"Unable to scan the content"`

**`src/ClipboardInterceptor/ClipboardInterceptorService.cs`**:
- `BlockFallback` → `"[DLP] Content blocked"`
- `BuildBlockText` non-empty branch → `$"[DLP] Blocked: {reason}"`
- `DlpMarker = "[DLP"` is unchanged (the loop-guard prefix; both strings still start with it).

**`interceptors/browser/addon.py::_notify_blocked`** — `guidance` →
`"Action: Please RELOAD (refresh) the page and STOP/CANCEL this upload. A blocked file may cause the browser to report a network error."`
(title/`File:`/`Reason:` already English.)

**`interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs`** (~line 134) →
`reason = "File blocked by security policy.";`

**`analyzer/policies.yaml`** `user_message` (your answer = translate):
- `block_visa_all_channels` → `"Credit card number (Visa) detected"`
- `block_cccd_all_channels` → `"Vietnamese Citizen ID (CCCD/CMND) detected"`
- `log_phone_numbers` → `"Phone number detected"`
- `block_confidential_keywords` → `"Confidential/internal document keyword detected"`
- `context_words`/`keywords`/`patterns` and the policy `name` fields are **unchanged**.
  (The Vietnamese comments are cosmetic; I will lightly translate the section header
  comments for readability but this is optional and changes no behavior.)

---

## 6. Sequential implementation tasks (do in this order)

Each task ends at a compiling/green checkpoint so a failure is localized.

1. **C-1 Translate code-level notifications** — `messages.py`, `ClipboardInterceptorService.cs`,
   `addon.py`, `OrchestratorClient.cs`, `policies.yaml`. *Checkpoint:* none yet (tests
   updated in step 5).
2. **A-1 Config refactor + hot-reload** — `config.py` (`_config_from_raw`,
   `apply_hot_reload`), `__main__.py` (`_handle_config_change`), `dispatcher.py`
   (`_analysis_timeout` property). *Checkpoint:* `import` smoke + pytest subset.
3. **B-1 Server cap move** — `policy_manager.py` (text → `max_extracted_chars`),
   `server.py` (ceiling helper), `config.py` (remove `max_clipboard_bytes` + add
   `clipboard_pipe_ceiling_bytes`), `config.yaml` (remove `clipboard.max_input_bytes`).
4. **B-2 Client cap removal** — `PipeAgentCore.cs`, `ClipboardConfigHolder.cs`,
   `Program.cs`. *Checkpoint:* `dotnet build` of the clipboard chain.
5. **T-1 Update + add tests** (§7.1 edits **and** §7.3 new test architecture: Layer A
   `test_config_apply_hot_reload.py`, Layer B `test_config_hot_reload_e2e.py`, Layer C
   `test_ctl_pipe.py` extension, §7.4 app_control).
6. **V-1 Run the full verification** (§8) and update the **README** (§9).

---

## 7. Test & doc updates required (so the build + suites stay green)

### 7.1 Tests that MUST change (they assert removed strings/behavior or build the config directly)

| File | Change | Reason |
|---|---|---|
| `src/AgentCore.Tests/ClipboardSelfWriteTests.cs` | Replace the `Assert.Contains('Đ', BuildBlockText(null))` / "Đã chặn" assertion with `Assert.StartsWith("[DLP", …)` + `Assert.Contains("Content blocked", BuildBlockText(null))`; switch the two sample reasons it passes to English text. | `BuildBlockText(null)` no longer contains `Đ` after C-1. |
| `scripts/harness/conftest.py` | Drop `max_clipboard_bytes` from the generated `limits:` (keep `max_file_bytes`). | Field removed from config in B-1. |
| `scripts/harness/test_session.py`, `test_supervisor.py`, `test_installer.py` | Remove the `max_clipboard_bytes=1,` kwarg from the direct `OrchestratorConfig(...)` constructions. | Field removed in B-1; otherwise `TypeError`. |
| `scripts/harness/test_failure_mode.py` | `test_oversize_follows_failure_mode` + `test_oversize_block_carries_friendly_message`: switch the trigger from `{"limits":{"max_clipboard_bytes":4}}` (text) to a **file** over `{"limits":{"max_file_bytes":4}}` (the `oversize` category now only applies to files). Update the friendly-message assertion to the **English** `messages.FAILURE_MESSAGES["oversize"]`. | Text oversize path replaced by `text_cap`; oversize category is now file-only; strings translated. |
| `scripts/harness/test_large_clipboard.py` | Repoint the "over the cap fails per failure_mode" case from `clipboard.max_input_bytes` to `analyzer.max_extracted_chars` (text > cap → `text_cap`). Keep the "large text under the cap is scanned in full and blocked on PII, not size" case (set a high `max_extracted_chars`). | Clipboard text is now governed by `max_extracted_chars`. |

### 7.2 Tests that do NOT need changing (self-contained Vietnamese — verified)

- `scripts/harness/test_events.py` and `src/AgentCore.Tests/PipeAgentCoreTests.cs` pass
  their **own** Vietnamese `user_message`/reason and assert it is echoed back — they test
  pass-through, not the shipped defaults, so they stay green. *(Optional cleanup: switch
  their sample strings to English for consistency; not required for green.)*
- `scripts/harness/test_analyzer_engine.py` uses Vietnamese **detection content**
  (context words/keywords/sample text) and a self-contained inline `user_message` fixture —
  unchanged.

### 7.3 New test architecture — EVERY field, BOTH directions

Your requirement: the suite must (i) prove **every hot-reloadable field actually
reloads**, and (ii) prove **every non-hot-reloadable field is actually inert on reload**.
A single behavioral test cannot do this — some fields have no pipe-observable behavior
(e.g. `drain_timeout_seconds`, worker-pool sizes) and the C# clients can't be driven from
the Python harness. So coverage is layered, and each field is assigned to the layer that
can assert it **deterministically**. The matrix below is the contract; the three test
files implement it.

> **Honesty about the boundary (no over-claiming):** the Python harness can prove (a) the
> orchestrator's *own* server-side consumption changes on reload, and (b) the
> orchestrator *broadcasts* the right client payload. It **cannot** prove the C# clients
> (ClipboardInterceptor / browser addon / Controller) *apply* what they receive, nor the
> Controller's runtime **rejection** of `shared_memory_name` — those are C#-side and are
> verified by the existing client logic + the VM/manual checks (§8 V-7, §9). Each such
> field is tagged **[client-apply: manual]** in the matrix.

#### Layer A — `test_config_apply_hot_reload.py` (NEW, in-process, exhaustive)

Pure unit test of `OrchestratorConfig.apply_hot_reload` — fast, deterministic, and the
**only** layer that touches *every* flat config field by name. Imports just
`orchestrator.config` (no analyzer deps). Build a baseline `OrchestratorConfig` from the
repo `config.yaml` (or a fixture raw dict), then:

- **HOT (parametrized, one case per field):** copy the baseline `raw`, change the field's
  source key to a sentinel value, call `apply_hot_reload(new_raw)`, then assert **(1)** the
  live attribute now equals the new value, and **(2)** the field name is in the returned
  `changed` list. Fields: each of the three `failure_mode` channels, `max_file_bytes`,
  `max_extracted_chars`, `supported_extensions`, `analysis_timeout_seconds`,
  `drain_timeout_seconds`.
- **NOT-OWNED-BY-`apply_hot_reload` (parametrized, one case per field):** copy the
  baseline `raw`, change the field's source key, call `apply_hot_reload(new_raw)`, then
  assert the live attribute is **unchanged** and the name is **not** in `changed`. This is
  the set `apply_hot_reload` deliberately leaves frozen — it splits into:
  - *Truly restart-only* (no running component re-reads them): `data_pipe`, `ctl_pipe`,
    `admin_pipe`, `clipboard_workers`, `browser_workers`, `peripheral_storage_workers`,
    `pipe_listeners`, `mitmdump_exe`, `addon_script`, `clipboard_exe`, `controller_exe`,
    `transfer_agent_exe`, `shell_extension_dll`, `payload_dll`, `log_dir`,
    `proxy_listen_port`, `proxy_bypass`, `policies_file`, `max_restarts`,
    `restart_window_seconds`, `stable_uptime_reset_seconds`, `app_control_enabled`,
    `app_control_inbox_dir`, `app_control_rejected_dir`, `app_control_staging_dir`,
    `app_control_reconcile_interval_seconds`, `app_control_extra_paths`.
  - *Reloaded by a DIFFERENT mechanism* (the App Control channel's `apply_config`, **not**
    `apply_hot_reload`): `app_control_poll_seconds`, `app_control_forward_block_events`.
    Layer A asserts only that `apply_hot_reload` does not touch them; their live reload is
    covered in §7.4.
- **Guard test:** even when `new_raw` carries a changed `data_pipe`/`ctl_pipe`/`admin_pipe`,
  `changed` never contains a pipe name (defense-in-depth — the primary guard is in
  `__main__`, Layer B).

> This layer is intentionally **complete over the dataclass**: the test enumerates
> `dataclasses.fields(OrchestratorConfig)` minus `raw` and asserts every name is in exactly
> one of two groups — **owned by `apply_hot_reload`** (the HOT list) or **not owned** (the
> two bullets above). A future field added without deciding its reload policy fails this
> meta-assertion. (`max_clipboard_bytes` is removed by Task B, so it is absent from both.)

#### Layer B — `test_config_hot_reload_e2e.py` (NEW, subprocess, behavioral)

Proves the **whole wire** (save `config.yaml` → `ConfigWatcher` → `_handle_config_change`
→ `apply_hot_reload` → live consumer) for every server-side field that has observable pipe
behavior. Uses `make_orchestrator` + rewrite `orch.config_path` + poll `pipe_send` (the
existing helper) until the new behavior appears (≤ ~3 s, mirroring `test_hot_reload.py`).
One test per field:

| Field | Setup → reload → expected |
|---|---|
| `clipboard.failure_mode` | small `max_extracted_chars`; over-cap text → BLOCK(`text_cap`); flip to `fail_open` → **ALLOW** |
| `peripheral_storage.transfer_agent.failure_mode` | same, `channel=peripheral_storage`; verdict flips BLOCK↔ALLOW |
| `browser.failure_mode` | same, `channel=browser` (unsupported-format file) → verdict flips |
| `analyzer.max_extracted_chars` | text just over a small cap → BLOCK(`text_cap`); raise cap → **ALLOW** (now scanned, clean) |
| `limits.max_file_bytes` | temp file over a tiny cap → BLOCK(`oversize`); raise cap → **ALLOW** |
| `analyzer.supported_extensions` | `foo.xyz` → BLOCK(`unsupported_format`); add `.xyz` → **ALLOW** |
| `service.analysis_timeout_seconds` | spawn with `DLP_TEST_SLOW_MS=400`, start `analysis_timeout_seconds: 0.1` → BLOCK(`timeout`); reload to `3.0` → **ALLOW** |

- **Non-reloadable, behavioral:** rewrite `config.yaml` with a **changed `data_pipe`**,
  wait for a reload cycle, then assert `pipe_send(orch.pipe_name, …)` on the **original**
  pipe still succeeds (the agent did not rebind) — and the agent logged a
  `data_pipe change requires restart` warning (assert via the orchestrator log file under
  `orch.log_dir`).
- `drain_timeout_seconds` has **no** steady-state pipe behavior (only observed during
  shutdown); it is covered by Layer A introspection and explicitly **not** given a flaky
  shutdown-timing test. Marked accordingly in the matrix.

#### Layer C — extend `test_ctl_pipe.py` (broadcast payload, all client fields)

Broaden the existing propagate test so a single save mutates **all** client-facing
hot-reloadable fields across **all three** components and asserts each arrives in the
broadcast, while the pipe names stay overridden:

- `clipboard`: `pipe_timeout_ms` **and** `failure_mode` (note: `max_input_bytes` is gone).
- `browser`: `pipe_timeout_ms` **and** `failure_mode`.
- `controller` (`peripheral_storage.controller`): `target_processes`, `failure_mode`,
  `payload_dll_path` — subscribe as component `controller` and assert the
  `peripheral_storage.controller` subtree in the pushed payload reflects the new values.
- Re-assert `data_pipe` **and** `ctl_pipe` are overridden back to in-use in every payload.

#### Field → layer coverage matrix (the contract)

| Field | Hot? | Layer A | Layer B | Layer C | Other |
|---|---|---|---|---|---|
| clipboard.failure_mode | yes | ✓ (server verdict) | ✓ | ✓ (broadcast) | [client-apply: manual] |
| clipboard.pipe_timeout_ms | yes | — | — | ✓ | [client-apply: manual] |
| browser.failure_mode | yes | ✓ | ✓ | ✓ | [client-apply: manual] |
| browser.pipe_timeout_ms | yes | — | — | ✓ | [client-apply: manual] |
| transfer_agent.failure_mode | yes | ✓ | ✓ | — (per-launch read) | [client-apply: manual] |
| controller.failure_mode / target_processes / payload_dll_path | yes | — | — | ✓ | [client-apply: manual] |
| controller.shared_memory_name | **no** (runtime-rejected) | — | — | — | [client-reject: manual/C#] |
| limits.max_file_bytes | yes | ✓ | ✓ | — | — |
| analyzer.max_extracted_chars | yes | ✓ | ✓ | — | — |
| analyzer.supported_extensions | yes | ✓ | ✓ | — | — |
| service.analysis_timeout_seconds | yes | ✓ | ✓ | — | — |
| service.drain_timeout_seconds | yes | ✓ | — (shutdown-only) | — | — |
| data_pipe / ctl_pipe / admin_pipe | **no** | ✓ | ✓ (data_pipe) | ✓ (override) | __main__ warning |
| pools.* / pipe_listeners | **no** | ✓ | — | — | — |
| paths.* (7 fields) | **no** | ✓ | — | — | — |
| proxy.listen_port / bypass | **no** | ✓ | — | — | — |
| policies_file | **no** | ✓ | — | — | content reloads via PolicyManager |
| supervisor.* (3 fields) | **no** | ✓ | — | — | — |
| app_control.enabled / *_dir | **no** | ✓ | — | — | — |
| app_control.poll_seconds / forward_block_events | yes (channel) | — | — | — | apply_config; §7.4 |
| install.* | n/a (install-time) | — | — | — | not runtime-read |

### 7.4 App Control reload (secondary)

`app_control.poll_seconds` / `forward_block_events` already hot-reload through the
channel's own `apply_config(new_raw)` (a different mechanism than `apply_hot_reload`). I
will confirm whether `test_app_control_*` already covers this; if not, add one assertion
that a `poll_seconds` change is applied (the channel exposes `set_poll_seconds`). This is
secondary to the orchestrator-core reload above.

---

## 8. Verification plan (detailed, with prerequisites and what I can pre-run)

> **Convention:** "PRE-RUN BY ME" = I will execute it during implementation and paste
> the result before handing back. "YOU RUN" = needs admin / a Developer PowerShell / the
> VM / real hardware that I cannot drive from here — marked with the reason.

### Prerequisites (one-time, from the repo root `D:\Code\GithubPublishEndpointDLP`)

- **P1.** Activate the dev venv: `.\.venv\Scripts\Activate.ps1` (or use the absolute
  interpreter `& "<RepoRoot>\.venv\Scripts\python.exe"`). Needed for every Python step.
- **P2.** `dotnet` on PATH (README confirms `C:\Program Files\dotnet\dotnet.exe`) — needed
  for the C# build/test steps. No admin required.
- **P3.** *(C++ only — NOT needed here)* A VS 2026 Developer PowerShell. **This plan
  touches no C++**, so `Payload.dll`/`DlpShellExt.dll` do **not** need rebuilding.

### Step V-1 — Python harness (orchestrator + analyzer)  ✅ PRE-RUN BY ME

```powershell
python -m pytest scripts\harness -q
```
*Expected:* the prior **145 passed** plus the new §7.3 cases — Layer A is heavily
parametrized (~6 hot fields + ~25 restart-only fields + guard + the "every field is
classified" meta-assertion) and Layer B adds ~7 behavioral cases, so expect on the order
of **+35–45** tests; the reworked `test_failure_mode`/`test_large_clipboard` stay net-flat.
**3 skipped** (admin-pipe) unchanged under a non-elevated shell. I will pre-run this, fix
until green, and put the **exact** final count here and in README §A.5 before handing back.
Prereq: P1. *(Runnable here — non-admin, matches README §A.5.)*

### Step V-2 — Python import/smoke of the changed modules  ✅ PRE-RUN BY ME

```powershell
python -c "import orchestrator.config, orchestrator.dispatcher, orchestrator.server, orchestrator.policy_manager, orchestrator.messages; print('import OK')"
python -c "from orchestrator.config import load_config; c=load_config('config.yaml'); print('ceiling', c.clipboard_pipe_ceiling_bytes()); print('changed', c.apply_hot_reload(c.raw))"
```
*Expected:* `import OK`, a numeric ceiling, and `changed []` (re-applying the same raw
changes nothing). Prereq: P1. *(Runnable here.)*

### Step V-3 — C# unit tests (AgentCore.Tests)  ✅ PRE-RUN BY ME (build env permitting)

```powershell
dotnet test src\AgentCore.Tests\AgentCore.Tests.csproj
```
*Expected:* `10 passed` (after the `ClipboardSelfWriteTests` update in §7.1). Prereq: P2.
*(README §A.5 ran this in a normal PowerShell with `dotnet` on PATH; I will attempt it
here and report. If the local SDK/NuGet restore is unavailable in this sandbox I will
mark it ⚠️ and you re-run.)*

### Step V-4 — C# build of the clipboard + transfer chain  ✅ PRE-RUN BY ME (build env permitting)

```powershell
dotnet build src\ClipboardInterceptor\ClipboardInterceptor.csproj -c Debug
dotnet build interceptors\peripheral_storage\TransferAgent\DlpTransferAgent.csproj -c Debug
```
*Expected:* both build (this transitively builds `AgentCore` + `DlpShared`). Prereq: P2.
*(Confirms the tuple/holder/Program.cs edits compile. Same note as V-3 about sandbox SDK
availability.)*

### Step V-5 — Browser addon import/parse smoke  ✅ PRE-RUN BY ME

```powershell
python -c "import ast; ast.parse(open('interceptors/browser/addon.py',encoding='utf-8').read()); print('addon parse OK')"
```
*Expected:* `addon parse OK` (the `_notify_blocked` edit is a string change only). A full
`import addon` requires `mitmproxy` + the addon's path setup, so I use `ast.parse` for a
dependency-free syntax check. Prereq: P1. *(Runnable here.)*

### Step V-6 — Foreground hot-reload smoke (functional)  ⚠️ PARTIALLY PRE-RUN BY ME

`test_config_hot_reload` (V-1) already exercises the reload path headlessly. If you want a
manual look:
```powershell
# Terminal 1 (venv): run the orchestrator in foreground against a scratch config
python -m orchestrator --foreground --config config.yaml
# Terminal 2: edit config.yaml (e.g. analyzer.max_extracted_chars), save, and watch
#             "config hot-reload applied: max_extracted_chars" appear in the console.
```
*I can pre-run the headless equivalent (V-1).* The interactive two-terminal form is
**YOU RUN** if desired (it just observes the same log line). No admin needed; foreground
does **not** exercise the USB hook or browser proxy (see README §A.5 note).

### Step V-7 — Installed/end-to-end manual checks  ⚠️ YOU RUN (cannot pre-run here)

The following need an **elevated** shell + a real install (and a removable drive / browser /
multiple user sessions). I cannot drive them from this environment, and they must not run
against your dev box without your intent:

- **Install:** `python -m orchestrator --install --config config.yaml` (elevated) →
  `Get-Service DLPAgent` Running → `dlp-ctl status` (new elevated shell).
- **Hot-reload, live:** edit `C:\Program Files\DLP\config.yaml`
  `clipboard.failure_mode: fail_open` → `dlp-ctl reload` → confirm
  `config hot-reload applied: failure_mode` in `dlp-agent.log`. *(This is the headline
  acceptance check for Task A on the installed service.)*
- **Clipboard, English + no cap:** copy a card+context string → clipboard replaced with
  `[DLP] Blocked: Credit card number (Visa) detected` (English). Copy the **entire**
  contents of a very large `.txt` (e.g. > the old 8 MB; well under `max_extracted_chars`)
  → analyzed in full, blocked/allowed on content, **not** size-rejected. Set
  `analyzer.max_extracted_chars: 100`, `dlp-ctl reload`, copy a longer text → BLOCK
  (`reason=text_cap`); add `clipboard.failure_mode: fail_open`, `dlp-ctl reload`, copy
  again → ALLOW (this is the Task A + Task B acceptance check, replacing the old
  `max_input_bytes` demo). Restore values + reload when done.
- **Browser:** upload a card+context file via Drive/Gmail → popup shows the English
  reason + the English reload/stop guidance.
- **USB / Transfer Agent:** "Transfer to USB (DLP Protected)" on a CCCD-with-context file
  → Note column shows the English reason; a pipe failure shows
  `"File blocked by security policy."`.
- **Uninstall:** `python -m orchestrator --uninstall --config config.yaml` (elevated).

> **Marked NOT pre-testable by me, with reason:** all of V-7 (needs Administrator, HKLM /
> LocalMachine cert store / service control, a removable drive, a browser with the CA
> trusted, and fast-user-switch) and the C++ artifacts (untouched, so not rebuilt).

---

## 9. README updates required (and how each is verified)

The README is the teammate's build/test manual, so it must match the new behavior. Edits:

1. **§A.8 step 3 (Clipboard)** — replace "the clipboard is replaced with `[DLP] Đã chặn:
   <reason>`" → the English `[DLP] Blocked: <reason>`. Replace the
   `clipboard.max_input_bytes` "size gate" sentence with: clipboard text is now sent in
   full and the analyzer scans it iff its length ≤ `analyzer.max_extracted_chars`; larger
   text is refused with `reason=text_cap` and follows `clipboard.failure_mode`.
2. **§A.8 step 5 (Failure mode demo)** — rewrite the `max_input_bytes: 100` oversize demo
   to the new `max_extracted_chars: 100` → `text_cap` demo (BLOCK; then `fail_open` →
   ALLOW), and note the `dlp-ctl reload` now re-applies **server-side** analyzer knobs
   too (not just the client). Drop the "near-cap copy must finish in time" wording tied to
   the byte cap.
3. **§A.8 step 1/2 (USB/Browser)** — change the example reasons to English; the browser
   popup carries the English reload/stop guidance.
4. **Appendix "config quick reference"** — remove `clipboard.max_input_bytes`; in the
   `clipboard` bullet keep `pipe_timeout_ms` + `failure_mode`. Update the line that says
   `supported_extensions` "restart to apply" → now hot-reloadable. Update the
   `failure_mode` paragraph: server-side failure_mode/limits/analyzer knobs are now
   hot-reloadable via `dlp-ctl reload`/save (previously restart-only).
5. **§A.9 (events.jsonl)** — `reason` token list is unchanged (`text_cap` already listed);
   note clipboard oversize now reports `text_cap` (not `oversize`).
6. **§A.5 expected pytest count** — bump `145 passed` to the new total (the §7.3 config-
   reload test layers add ~35–45 cases; I will write the exact number after the V-1
   pre-run), `3 skipped` unchanged. Add a sentence that the harness now also exercises
   **config** hot-reload (both that hot-reloadable fields apply and that restart-only
   fields stay inert), alongside the existing **policy** hot-reload.

*Verification of README steps:* the **non-admin** ones (§A.5 pytest count, the
config-reference facts) I will re-run/confirm. The **install/VM/manual** README steps
(§A.7/§A.8/§B) are the same family as V-7 — **YOU RUN / mark ⚠️ NOT PRE-TESTED**, with the
reason already stated (admin + VM + hardware). I will tag each edited README step with the
correct ✅/⚠️ marker consistent with the existing convention.

---

## 10. Risks & notes

- **Memory on the 8 GB VM.** Removing the 8 MB clipboard cap raises the worst-case
  per-clipboard pipe buffer to `max_extracted_chars*4 + 1 MB` (~64 MB at the default cap),
  ×2 clipboard workers. That is in line with the existing per-file analysis budget
  (`max_extracted_chars` already implies a few hundred MB/analysis) and bounded by the
  ceiling helper. If you prefer a tighter clipboard buffer, lower `max_extracted_chars`
  (now hot-reloadable) or we can cap the ceiling at a fixed smaller value — say so and I'll
  adjust §4.3.
- **No C++ changes.** `Payload.dll` / `DlpShellExt.dll` are untouched; no Developer
  PowerShell / MSBuild needed for this change. The deploy bundle only needs the rebuilt
  C# artifacts (`package-bundle.ps1` re-copies them).
- **Back-compat removed for `limits.max_clipboard_bytes` / `clipboard.max_input_bytes`.**
  Both keys become no-ops (ignored if present). Old configs still parse; the keys simply
  do nothing. The README + `config.yaml` drop them.
- **Atomicity of in-place config swaps** is guaranteed by the GIL for single
  attribute/reference assignments (§A.1) — no new lock is introduced, matching the
  existing lock-free reads in `Dispatcher`/`PolicyManager`.
- **`analysis_timeout_seconds` invariant** (client pipe timeout must exceed it) is now
  reload-time relevant; documented in §2 and the README, not auto-enforced (consistent
  with today).

---

## 11. Open questions for you (only if any of these are wrong)

1. The English wording in §5 — happy with it, or do you want different phrasing/length
   (e.g. shorter clipboard text, or include a support contact)?
2. §10 memory trade-off — keep the `max_extracted_chars`-derived clipboard ceiling, or
   pin a smaller fixed ceiling?

Everything else is decided by your three earlier answers. On your go-ahead I implement
in the §6 order, pre-run V-1…V-5 (and V-6 headless), and update the README, marking any
step I could not execute with its reason.
