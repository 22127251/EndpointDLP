# Phase A — Stabilize the Orchestrator's Phase-2 Work (clear-language version)

> This is a re-worded version of the plan at `C:\Users\PocketBaguette\.claude\plans\code-base-brief-this-tender-wall.md` — same decisions, same bugs, same implementation tasks, just written more carefully so it can be read in isolation. The original plan was the one used to drive the implementation; this version exists for re-reading and future reference.

## Context

The DLP endpoint agent is built from four pieces that were developed somewhat independently:

- the **analyzer** (Python — content scanner that decides ALLOW / ALLOW-and-log / BLOCK on a piece of text or a file),
- three **interceptors** (browser via mitmproxy, clipboard via C#, peripheral_storage via C++/C#),
- and the **orchestrator** (Python — a named-pipe server that takes intercepted content from the interceptors, runs it through the analyzer, and returns a decision over the same pipe).

These pieces have been merged into one tree. On a basic end-to-end test, the agent works: an interceptor sends a payload, the orchestrator analyzes it, and a decision comes back. That basic end-to-end test is what we mean by the **"happy path"** — everything goes right, only one request is in flight at a time, no one is editing the policies, the analyzer returns quickly, and the user doesn't do anything weird like copy three things in a row.

What has **not** been tested is the orchestrator's behavior under stress (many requests at once, policies changing while requests are in flight, a slow analyzer, rapid-fire clipboard copies). The earlier integration plan (`integreation-plan2.md`) calls out **four specific orchestrator behaviors** that the Phase 0–2 code *claims* to support but were never verified end-to-end. Each one is a place where, if the behavior doesn't actually hold, the agent could leak sensitive data, hang the user, or silently use stale policies. The four behaviors are:

1. **Multi-instance pipe concurrency** (`orchestrator/server.py`).
   The orchestrator runs N "accept threads" (default 4) on a single named pipe so it can serve multiple interceptor requests at the same time. Under load (e.g. mitmproxy fanning out uploads while the user copies text while a USB transfer is in flight), all of those parallel requests must each get the correct response — no dropped responses, no deadlocks, no responses delivered to the wrong client.

2. **Policy hot-reload under load** (`orchestrator/policy_manager.py`).
   The orchestrator watches `analyzer/policies.yaml` and rebuilds the analyzer engine whenever the file changes, so the admin doesn't have to restart the agent to push a new rule. The risk is what happens when that rebuild races a live analysis request: does the request crash? Does it see a half-built engine? Does the new policy actually take effect for **subsequent** requests, or does the orchestrator keep using the old engine for some unspecified window?

3. **Dispatcher fail-closed timeout** (`orchestrator/dispatcher.py`).
   The dispatcher gives each analysis at most `_ANALYSIS_TIMEOUT = 4` seconds. If the analyzer takes longer than that (a stuck regex, a giant file, anything), the dispatcher must return `BLOCK` to the interceptor — **not** silently let the content through, **not** hang the interceptor waiting for an answer. "Fail-closed" means: when in doubt, block. The claim is that the dispatcher honors this; we need to prove it does, and we need to confirm the client side actually receives that BLOCK rather than timing out on its own.

4. **Clipboard supersession edge cases** (`orchestrator/dispatcher.py`).
   If the user copies content A and then immediately copies content B before A finishes analyzing, the orchestrator should (a) drop the in-flight response for A — it would just overwrite the clipboard with a stale decision — and (b) make sure B gets a real response. The "edge cases" are the small-window race conditions around this: at what moment is A's cancel flag set vs. when A's analysis actually finishes; what happens to A's pipe handle once the orchestrator decides to drop the response; whether B can also be superseded before it gets to run.

Phase A's job is to **prove these four behaviors hold under stress**, and **fix the real bugs found while doing so**, before any new functionality (Phase B onward) is added. No new orchestrator features.

## Locked decisions (this session)

> Cross-reference key:
> - **"Gap 1 / 2 / 3 / 4"** refers to the four numbered behaviors in the Context section above (1 = concurrency, 2 = hot-reload, 3 = timeout, 4 = supersession).
> - **"B1, B2, B5, B7, …"** are the bug IDs assigned in the *Bug list* section further down.

| # | Decision |
|---|----------|
| 1 | Harness lives in `scripts/harness/` (alongside `scripts/run_*.ps1`). |
| 2 | Harness uses **pytest** (new dev dependency). We write **one test file per gap** — "gap" here means one of the four behaviors from the Context section above. Concretely: `test_concurrency.py` exercises Gap 1, `test_hot_reload.py` exercises Gap 2, `test_timeout.py` exercises Gap 3, `test_supersession.py` exercises Gap 4. |
| 3 | The bugs we found in the four files that the harness exercises are fixed in this phase. Concretely, four bugs (described in detail in the **Bug list** section further down) are in scope: <br>• **B1** — `interceptors/browser/pipe_client.py` doesn't actually enforce its own documented `timeout_seconds` on `ReadFile`. <br>• **B2** — `src/AgentCore/PipeAgentCore.cs` doesn't have a deadline that covers the whole connect→write→read exchange; only the initial connect is bounded. <br>• **B5** — `orchestrator/policy_manager.py` misses atomic-save events (`on_moved`), so editors that write-to-temp-then-rename silently break hot-reload. <br>• **B7** — `orchestrator/policy_manager.py` doesn't coordinate the engine snapshot read with the reload swap, so a request that arrives **after** a save can still happen to use the old engine. |
| 4 | Hot-reload bar is **strict**: every request that begins after `policies.yaml` is saved must use the new engine. Acceptable to add a lock on the analyze hot path. |
| 5 | A `--config PATH` flag is added to `orchestrator/__main__.py` (test-only affordance, harmless in prod; required so the harness can drive an isolated orchestrator without clobbering the dev's running one). |
| 6 | Slow-analysis mechanism: first try (a) generated 50 MB extract-able file; if any test flakes from timing, fall back to (b) a `DLP_TEST_SLOW_MS` env-var monkey-patch on `PolicyManager.analyze`. |
| 7 | C# `PipeAgentCore.cs` cancellation fix is verified with a new **xUnit test project** at `src/AgentCore.Tests/` (target `net10.0-windows`, xunit v3 3.2.x). |
| 8 | Out-of-scope: making `future.cancel()` actually kill running pool work (requires analyzer to be cancellable). Log a note instead. |

## Bug list (with file:line refs)

### Will be fixed in Phase A

**B1. `interceptors/browser/pipe_client.py:50–54`** — the function's docstring at the top of the file promises *"Raises TimeoutError if no response within timeout_seconds"*, but the actual `ReadFile` call is **unbounded** — meaning it has no time limit and will sit there waiting forever for the orchestrator to write a response. At lines 50–54 the code does compute the time remaining:

```python
remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
```

but then **`remaining_ms` is never used** — `win32file.ReadFile(handle, 64 * 1024)` is called without it. Concretely: if the orchestrator hangs **after** the client successfully connects (rather than refusing the connection in the first place), the client blocks forever instead of raising `TimeoutError` after `timeout_seconds`. That is the "docstring contract violation" — the function's documented behavior is *"give up after N seconds"*, but the actual code behavior is *"give up only if the pipe isn't there at all; once we're connected, wait forever."*

Fix with overlapped I/O (see Implementation task IT2 below): turn `ReadFile` / `WriteFile` into asynchronous calls and explicitly wait on the completion event with the remaining time as the wait timeout.

**B2. `src/AgentCore/PipeAgentCore.cs:19–44`** — `ConnectAsync(_timeoutMs, ct)` honors the 5 s budget only for connect. `WriteAsync` / `FlushAsync` / `ReadAsync` honor only the caller-supplied `ct`. With the parameter default `ct = default`, there is no timeout. Even when `ClipboardInterceptorService` passes `_currentCts.Token`, that token only fires on a *new* clipboard copy — a wedged orchestrator hangs the call indefinitely. Fix with a linked CTS that has overall `CancelAfter(_timeoutMs)`. See IT3.

**B5. `orchestrator/policy_manager.py:25–33`** — `_ReloadHandler` only handles `on_modified`. Atomic-save editors (write-temp + rename) fire `on_moved` on `policies.yaml` and the reload never triggers. Hot-reload silently breaks for that workflow. Add `on_moved` handler. See IT1.

**B7. `orchestrator/policy_manager.py:74`** — `engine = self._engine` is a snapshot without any synchronization with `_reload_engine` (line 57). A request whose analyze() entry happens *after* the user saves `policies.yaml` can still observe the old engine if the snapshot wins the race against the swap. Under the strict bar this is a violation. Fix by acquiring a lock around both the swap and the snapshot read. See IT1.

### Found but NOT fixed in Phase A (documented only)

**B3.** `orchestrator/server.py:98` — `_stop.is_set()` check is racy with `stop()` throwaway connects. Benign during shutdown; client retries cover it.

**B4.** `orchestrator/server.py:54–64` — `stop()` only wakes `pipe_listeners` accept loops; verified that `CreateFile` against a busy pipe instance returns `ERROR_PIPE_BUSY` immediately, so the swallow at the `except` is correct. Not a bug.

**B6.** `orchestrator/policy_manager.py:54–60` — `_reload_engine` is not serialized against itself. Subsumed by the IT1 lock.

**B8.** `orchestrator/dispatcher.py:80, 102, 132` — `future.cancel()` returns `False` for running futures. Stuck analyses keep occupying pool slots. Add a log line `pool=<name> queued=<N>` when timeouts fire, so we can detect zombie buildup, but no behavior change. Real fix requires analyzer-level cancellation (Phase F territory).

**B9.** `orchestrator/dispatcher.py:108–146` — hypothesis was: between releasing `_clip_lock` (line 117, where seq is inserted into `_clip_inflight`) and entering the `try` at line 119, an exception could leak the seq in `_clip_inflight`. Verified non-bug: there is no executable statement between the lock release and the `try:` line, and the `try/finally` at 119/137–139 unconditionally pops the seq in `finally`. Even `pool.submit` raising (e.g., on shutdown) is caught.

**B10.** `orchestrator/dispatcher.py:142–143` — the supersession log reads `self._clip_seq` outside `_clip_lock`. Verified non-bug: in CPython, reading an `int` attribute is atomic under the GIL, so no torn read. The value is only included in a log message; behavior is gated by `cancel_flag.is_set()` (a `threading.Event`, which is internally synchronized), not by `self._clip_seq`. Worst-case staleness: an off-by-one logged value.

## Implementation tasks

### IT0. Add `--config PATH` flag to `orchestrator/__main__.py`

- Add `parser.add_argument("--config", type=Path, default=None, help="Path to orchestrator.yaml (defaults to repo root).")` to the argparse setup.
- Thread `args.config` into `_run_foreground(args.config)` → `load_config(args.config)`. `config.load_config` already accepts an optional `path` (config.py:31), so this is a 3-line plumbing change.

### IT1. `orchestrator/policy_manager.py` — strict reload + atomic-save handling

Reference existing reuse points: `DLPEngine(self._policies_file)` already validates YAML; its constructor raises on parse errors. The existing `except Exception` in `_reload_engine` keeps the old engine — good defense-in-depth, leave it.

Changes:

1. Add `self._engine_lock = threading.Lock()` in `__init__`.
2. Rewrite `_reload_engine`:
   - Construct `new_engine = DLPEngine(self._policies_file)` **outside** the lock (slow, can be 50–200 ms).
   - Only the assignment `self._engine = new_engine` is inside `with self._engine_lock:`.
   - Keep the `except Exception` wrapper around the constructor call so a half-written YAML doesn't crash the reload thread.
3. In `analyze`, replace `engine = self._engine` with `with self._engine_lock: engine = self._engine` (line 74). Lock-held time is one pointer copy.
4. Add `on_moved` to `_ReloadHandler`, dispatching to the same debounce path as `on_modified`. Extract the debounce logic into `_schedule_reload(self)` so both `on_modified` and `on_moved` call it.
5. Shrink the debounce from 500 ms → 100 ms (the constructor outside the lock is the real cost; 100 ms is still enough to coalesce duplicate `on_modified` events from a single save on Windows).

Why this gives the strict guarantee: by Python's memory model, `Lock.release` happens-before any subsequent `Lock.acquire`. So once `_reload_engine` exits, any analyze() that acquires the lock after that observes the new engine. Analyze() calls that already passed the lock-grab use the engine they captured — which is correct ("started before the save").

### IT2. `interceptors/browser/pipe_client.py` — overlapped I/O with deadline

Replace the synchronous `ReadFile`/`WriteFile` with overlapped I/O so the docstring's promised `TimeoutError` actually fires.

- Open the handle with `FILE_FLAG_OVERLAPPED` (`win32con.FILE_FLAG_OVERLAPPED = 0x40000000`).
- For each I/O: create a `pywintypes.OVERLAPPED` with `hEvent = win32event.CreateEvent(None, True, False, None)`; call `WriteFile` / `ReadFile` with the OVERLAPPED; `win32event.WaitForSingleObject(ov.hEvent, remaining_ms)`; on `WAIT_OBJECT_0` call `win32file.GetOverlappedResult(handle, ov, False)`; on `WAIT_TIMEOUT` call `win32file.CancelIoEx(handle, ov)` and raise `TimeoutError`.
- Allocate the read buffer with `win32file.AllocateReadBuffer(64 * 1024)`.
- Risk: pywin32's `WriteFile` returns `(0, ERROR_IO_PENDING)` in async mode; some versions raise on it. Mitigation: at the start of implementation, do a ~30-min spike on a hello-world pipe to confirm the pattern works before touching the real client. If it doesn't, fall back to a watchdog-thread approach (spawn a thread that sleeps `remaining_ms` then closes the handle to unblock the synchronous ReadFile; the success path cancels the watchdog before close).

Match a single canonical helper `_wait_or_cancel(handle, ov, deadline) -> n_bytes` so write and read paths are symmetric.

### IT3. `src/AgentCore/PipeAgentCore.cs` — overall cancellation deadline

Inside `AnalyseAsync`, create `using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct); cts.CancelAfter(_timeoutMs);` and pass `cts.Token` to every I/O (ConnectAsync, WriteAsync, FlushAsync, ReadAsync). Two catch arms:

```csharp
catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
catch (OperationCanceledException) { return AnalysisDecision.Block; }
```

The first arm preserves the caller-driven cancellation semantics that `ClipboardInterceptorService` relies on (line 79 catches `OperationCanceledException` and treats it as supersession). The second arm is the deadline-exceeded fail-closed path.

Note: this changes `_timeoutMs` from "connect timeout" to "overall budget." Consider bumping the default from 5000 ms → 6000 ms (1 s margin above the orchestrator's 4 s analysis cap + connect overhead). Flag the semantic change in the commit message.

### IT4. New pytest harness at `scripts/harness/`

Layout:

```
scripts/harness/
  conftest.py                  # fixtures (orchestrator subprocess, pipe helpers, tmp config)
  pipe_helpers.py              # pipe_send(payload, timeout) — same semantics as fixed pipe_client.py
  fixture_policies/
    permissive.yaml            # zero policies — everything ALLOWs fast
    visa_block.yaml            # one block_visa policy for the hot-reload flip test
    slow.yaml                  # OR: paired with the 50 MB file to drive >4 s analyses
  fixture_files/               # built at session-scope: a 50 MB .txt for the slow path
  test_concurrency.py          # gap 1
  test_hot_reload.py           # gap 2 (strict bar)
  test_timeout.py              # gap 3
  test_supersession.py         # gap 4
  pytest.ini                   # testpaths, timeout, markers
  requirements.txt             # pytest>=8.0, pytest-timeout>=2.3
  README.md                    # one paragraph: how to invoke (do NOT add unless user asks)
```

Plus at repo root: `requirements-dev.txt` containing `-r requirements.txt`, `-r analyzer/requirements.txt`, `pytest>=8.0`, `pytest-timeout>=2.3`. Keeps prod and dev deps separate.

Invocation (normal PowerShell, repo root):

```
python -m pytest scripts/harness/ -v
```

#### Fixtures (`conftest.py`)

- **`orchestrator_process`** (function-scoped): builds a unique pipe name `\\.\pipe\dlp_test_{pid}_{uuid}`, writes a per-test `orchestrator.yaml` and `policies.yaml` under `tmp/harness/<uuid>/`, launches `python -m orchestrator --foreground --config <path>` with `creationflags=CREATE_NEW_PROCESS_GROUP`, polls `WaitNamedPipe` (max 10 s) until ready, yields a small object exposing `pipe_name`, `policies_path`, `config_path`, `proc`. Teardown sends `signal.CTRL_BREAK_EVENT`, waits 5 s, then `proc.kill()`, then `shutil.rmtree(tmp_dir, ignore_errors=True)`. Per-test scope is chosen for isolation; total suite runtime ~10 s for 4 tests.
- **`pipe_client`**: thin wrapper over `pipe_helpers.pipe_send` bound to the running orchestrator's pipe name.
- **`policies_helpers`**: exposes `write_policies(yaml_str)` that does `os.replace` (atomic write-temp + rename) so the test exercises the `on_moved` path from B5/IT1.

#### Per-test plans

- **`test_concurrency.py`** (gap 1): orchestrator with `pipe_listeners=4, browser_workers=3, policies=permissive`. Launch 16 parallel `pipe_send` calls via `ThreadPoolExecutor(16)`, each with a unique payload UUID. Assert all 16 return `("ALLOW", "")` within 8 s, no exceptions.
- **`test_hot_reload.py`** (gap 2, strict): orchestrator starts with `permissive.yaml`. Verify a baseline visa-number probe returns ALLOW. Atomically replace policies with `visa_block.yaml`. Poll: probe again every 50 ms (max 1 s) until BLOCK is observed → swap confirmed. Immediately fire another fresh visa-number request — assert BLOCK. Variant: also write `policies.yaml` via direct `open().write()` (non-atomic) to exercise `on_modified` path. Both must converge.
- **`test_timeout.py`** (gap 3): orchestrator with `browser_workers=1` and a 50 MB text file built once at session scope. Send `kind=file` request → assert `("BLOCK", "Analysis timed out")` within 5 s. If timing is flaky, switch this test to the `DLP_TEST_SLOW_MS=5000` env-var path (which requires the orchestrator `--foreground` to honor the env var by monkey-patching `PolicyManager.analyze`; ~5 extra lines in `__main__.py` behind `if os.environ.get("DLP_TEST_SLOW_MS"):`).
- **`test_supersession.py`** (gap 4): orchestrator with `clipboard_workers=1`. Fire 3 clipboard requests in quick succession on 3 threads against a slow path; assert exactly one returns a normal `(decision, reason)` tuple and the other two raise (broken pipe / timeout). Don't assert which one wins — that's wire-order dependent.

#### `pytest.ini`

```
[pytest]
testpaths = scripts/harness
timeout = 30
timeout_method = thread
markers =
    slow: tests that exceed 5s
```

### IT5. New xUnit test project at `src/AgentCore.Tests/`

`src/AgentCore.Tests/AgentCore.Tests.csproj` targets `net10.0-windows`, references `..\AgentCore\AgentCore.csproj`, and pulls in `xunit.v3` 3.2.x + `Microsoft.NET.Test.Sdk` (xunit v3 3.2.2 is the current stable as of 2026-05; supports .NET 10 SDK per the v3.2.2 release notes).

Single test class with two cases:

- **`AnalyseAsync_HangingServer_BlocksWithinDeadline`**: spin up a tiny `NamedPipeServerStream` on a unique pipe name that accepts the connection but never writes a response. Construct `PipeAgentCore(pipeName, timeoutMs: 1500)`. Assert `await client.AnalyseAsync("data", default)` returns `AnalysisDecision.Block` and completes within ~2 s. Without the IT3 fix, this hangs.
- **`AnalyseAsync_UserCancellation_PropagatesOpCanceled`**: same hanging server. Use `var cts = new CancellationTokenSource(); cts.CancelAfter(200);`. Assert `OperationCanceledException` is thrown (not swallowed as Block) — confirms the `when (ct.IsCancellationRequested)` arm preserves caller-driven cancellation.

Run from Visual Studio 2026 Developer PowerShell:

```
dotnet test src\AgentCore.Tests\AgentCore.Tests.csproj
```

(`dotnet test` is the right command for C# .NET projects per project memory; MSBuild is only for `.vcxproj`.)

No solution-file change required: `src/AgentCore/` and `src/ClipboardInterceptor/` are already built independently by `dotnet build` against their own `.csproj`. The new test project follows the same pattern.

## Critical files

**Code edits**
- `orchestrator/__main__.py` — IT0 (`--config` flag, optional `DLP_TEST_SLOW_MS` patch)
- `orchestrator/policy_manager.py` — IT1 (lock + on_moved + 100 ms debounce)
- `interceptors/browser/pipe_client.py` — IT2 (overlapped I/O)
- `src/AgentCore/PipeAgentCore.cs` — IT3 (linked CTS with CancelAfter)

**Reused (no edits)**
- `orchestrator/config.py:31` — `load_config(path=None)` already accepts a path; IT0 wires it.
- `analyzer/engine.py` `DLPEngine.__init__` — already raises on bad YAML; IT1 relies on this.
- `orchestrator/dispatcher.py` — unchanged in Phase A; B8 noted but not fixed.
- `orchestrator/server.py` — unchanged in Phase A.

**New files**
- `requirements-dev.txt` (repo root)
- `scripts/harness/` (whole tree, see IT4)
- `src/AgentCore.Tests/` (whole project, see IT5)

## Risks and follow-ups

- **R1 (IT2):** pywin32 overlapped I/O on named pipes is well-documented but not exercised in this codebase yet. Mitigation: 30-min spike at start of IT2 to validate the pattern; fall back to watchdog-thread if needed.
- **R2 (IT4 slow file):** 50 MB extract timing varies by machine. If `test_timeout.py` flakes, switch to env-var path (already scoped via decision #6).
- **R3 (IT5 hanging server):** xUnit's default ~30 s per-test timeout is fine; assert with `Stopwatch` to catch a hang explicitly rather than relying on the framework timeout.
- **R4 (deferred):** B8 zombie pool workers — add the `pool=<name> queued=<N>` log line in IT1's `_analyze_*` timeout arms only if it's truly a one-liner; otherwise defer to Phase F. Re-evaluate during implementation.
