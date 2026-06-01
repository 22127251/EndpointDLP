# Phase B — Unify all non-policy configuration into one sectioned `config.yaml`

> Cross-reference key:
> - **IT-B1 … IT-B8** are the eight implementation tasks defined in the *Implementation tasks* section below. Risks, files, and verification steps refer back to them.
> - **Q1 … Q4** are the user-confirmed Phase B decisions (Controller config, browser config, C# clients, hot-reload model).
> - **D1 … D4** are the four follow-up design choices (shared C# helper placement, pipe-name hot-reload semantics, PipeAgentCore mechanism, TransferAgent locator).
> - **R1 … R8** are the risks tracked in the *Risks and follow-ups* section.

## Context

Phase A stabilized the orchestrator under stress (multi-instance pipe concurrency, policy hot-reload, fail-closed timeout, clipboard supersession) and fixed B1/B2/B5/B7. The agent now works end-to-end on the happy path AND under load. But the codebase still has **three** non-policy config files:

- `orchestrator.yaml` at repo root (orchestrator-only fields, plus many declared-but-unused placeholders intended for Phases C–F). Phase B **renames this to `config.yaml`** (see locked decision #12) since it now centralizes config for every component, not just the orchestrator. To save churn in the rest of this plan, every subsequent mention of `config.yaml` at repo root refers to the post-rename name.
- `interceptors/peripheral_storage/Controller/Config/config.yaml` (Controller's own copy of target processes / fail mode / shared-memory name / payload DLL path). **Deleted in IT-B8** — Controller starts reading the central file instead.
- `interceptors/browser/config.yaml` (mitmproxy addon's pipe name, timeouts, fail behavior, ext/MIME allowlists, domain blocklist, upload-URL keywords). **Deleted in IT-B5.**

…plus **hardcoded** pipe names and timeouts in two C# clients:

- `src/AgentCore/PipeAgentCore.cs:13` — `"dlp_agent"`, 6000 ms (ClipboardInterceptor reads these defaults).
- `interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs:23-25` — `"dlp_agent"`, 5000 ms connect, 10 s analysis.

Phase B unifies all of this into one sectioned `config.yaml` so that every component reads its labelled section from a single source. `analyzer/policies.yaml` stays separate (policy ≠ config — see Phase A's locked decision #4 carried forward).

**Hot-reload model** (Q4): the orchestrator owns a `FileSystemWatcher` on `config.yaml` and pushes per-section updates to subscribed clients over the control pipe `\\.\pipe\dlp_agent_ctl` (a duplex, message-mode Windows named pipe; "ctl-pipe" hereafter). Clients hold one long-lived subscription each. This pipe is declared in `config.yaml` today but never opened — Phase B opens it and defines a minimal subscribe/push protocol (see *Ctl-pipe protocol* below).

**Outcome:** at the end of Phase B, every long-running component (orchestrator, ClipboardInterceptor, browser addon, Controller) reads its initial config from `config.yaml` at startup and receives live updates via the ctl-pipe. TransferAgent (short-lived per-file) does a one-shot disk read at startup only. The three legacy config files are deleted.

## Locked decisions (this session)

| # | Decision | Source |
|---|----------|--------|
| 1 | Single `config.yaml` is the source of truth for all non-policy config. `analyzer/policies.yaml` stays separate. | Carried forward from integration-plan2.md decision #4 |
| 2 | Controller reads the central `config.yaml` directly (no installer-synced shadow copy). The legacy `Controller/Config/config.yaml` is deleted; `AppConfig.cs` is repurposed to deserialize the `peripheral_storage` section from the central file. | Q1=B |
| 3 | The browser addon (mitmproxy) reads `config.yaml` directly. `interceptors/browser/config.yaml` is deleted; `interceptors/browser/config.py` becomes a thin loader of the `browser:` section. | Q2=B |
| 4 | ClipboardInterceptor and TransferAgent gain YamlDotNet 17.0.1 and read `config.yaml` directly. Pipe names and timeouts come from yaml, not hardcoded constants. | Q3=1 |
| 5 | Hot-reload is **centralized**: orchestrator watches `config.yaml`, then pushes per-section JSON updates over the ctl-pipe to each registered subscriber. Bootstrap is still a direct disk read (so clients can start without the orchestrator running yet). | Q4=2 |
| 6 | Shared C# code (path discovery + ctl-pipe subscriber) lives in a **new small library** `src/DlpShared/`. Both `src/AgentCore/AgentCore.csproj` and `interceptors/peripheral_storage/Controller/Controller.csproj` and `interceptors/peripheral_storage/TransferAgent/DlpTransferAgent.csproj` add a `ProjectReference` to it. This avoids the cross-tree dep that Controller-→-AgentCore would have created and the unused-PipeAgentCore drag in TransferAgent. | D1=b |
| 7 | `data_pipe` and `ctl_pipe` are declared **non-hot-reloadable** at the field level, NOT at the broadcast level. If `config.yaml` changes either field, the orchestrator logs `"data_pipe/ctl_pipe change requires restart; keeping <old value>"`, then **continues broadcasting all OTHER changed fields** in the same save. The pushed payload has the unchangeable fields overridden back to their in-use values, so subscribers receive an internally-consistent snapshot. Clients also warn-and-ignore on receive if they ever observe a change to these fields. Exact same pattern as Controller's existing `shared_memory_name` rejection (Program.cs:120-128) — which rejects the immutable field but applies all other changes. | D2=a |
| 8 | `PipeAgentCore` gains a `Func<(string pipeName, int timeoutMs)> provider` constructor overload so that ClipboardInterceptor can flip its timeout live on a ctl-push without recreating the instance. The existing literal-value constructor is kept (used by `src/AgentCore.Tests/`). | D3=a |
| 9 | TransferAgent does **NOT** subscribe to the ctl-pipe — its lifecycle is per-file (launched by ShellExtension, exits when transfer completes). It does a one-shot disk read of `config.yaml` at startup via `DlpShared.ConfigLocator`. | D4 (follows from D6) |
| 10 | Path discovery for `config.yaml` uses a single env var `DLP_CONFIG_PATH`; on miss, walk up from the executable's directory looking for `config.yaml`, **N=8 levels deep** (enough for TransferAgent's deep `bin/Debug/net10.0-windows/win-x64/` path with headroom). On miss, exit non-zero with a clear error. mitmproxy addon also accepts `--set dlp_config_path=...` as belt-and-suspenders. | New |
| 11 | After Phase B is implemented and verified, the implementer edits `D:\Code\GithubPublishEndpointDLP\integration-plan2.md` to reflect the new state (mark Phase B done, update Current State Snapshot, remove resolved open questions 1 & 2, update file-name references from `orchestrator.yaml` to `config.yaml`). | User instruction this session |
| 12 | The central config file is **renamed from `orchestrator.yaml` to `config.yaml`** at repo root, because it is no longer orchestrator-only. To neutralize the false-positive risk a generic filename creates for the walk-up discovery, every path-discovery implementation (C# `DlpShared.ConfigLocator` and Python `interceptors/browser/config.py::find_config_yaml`) **requires the candidate file to contain a top-level `data_pipe:` key** (the sentinel) before accepting it. Without this sentinel, walking up from a deep `bin/Debug/...` could accidentally pick up an unrelated `config.yaml` that some other tool dropped between the executable and repo root. | User decision this session |
| 13 | **Single subscriber per component** on the ctl-pipe. Registry is `dict[str, Handle]`. A duplicate `subscribe` for a component that already has a live entry gets `{type:"error", code:"already_subscribed", ...}` and connection-close — loudly catching duplicate-launch dev mistakes. Reconnect-after-pipe-break self-resolves: the old worker thread sees EOF and prunes its handle before the reconnecting client's next attempt arrives (250 ms backoff is enough). Phase E will refactor to `(session_id, component)` keying when session-aware spawning is introduced. | User decision this session |

## Final unified `config.yaml` schema

Section layout principle: shared infrastructure (pipe names) stays top-level; everything a single component owns lives in that component's named section. Each section maps 1:1 to the component that reads it.

```yaml
# Source of truth for all non-policy DLP config.
# Policy rules live in analyzer/policies.yaml (intentionally separate).

# ── Shared infrastructure (consumed by orchestrator AND every client) ──
data_pipe: "\\\\.\\pipe\\dlp_agent"
ctl_pipe:  "\\\\.\\pipe\\dlp_agent_ctl"

# ── Orchestrator-only ──
pools:
  clipboard_workers:          2
  browser_workers:            3
  peripheral_storage_workers: 2
  pipe_listeners:             4

limits:
  max_clipboard_bytes: 1048576       # 1 MB
  max_file_bytes:      104857600     # 100 MB (browser)

supervisor:                          # Consumed in Phase C; harmless to keep now.
  max_restarts: 3
  restart_window_seconds: 60
  stable_uptime_reset_seconds: 60

paths:                               # Phase C/D will add more (controller_exe, etc.).
  mitmdump_exe: ""
  addon_script: "interceptors/browser/addon.py"
  clipboard_exe: "src/ClipboardInterceptor/bin/Debug/net10.0-windows/ClipboardInterceptor.exe"
  log_dir: ""

proxy:
  listen_port: 8080
  bypass: "localhost;127.0.0.1;<local>"

policies_file: "analyzer/policies.yaml"

# ── Per-component sections (each component reads ONLY its own subtree) ──

clipboard:
  pipe_timeout_ms: 6000              # was hardcoded in PipeAgentCore.cs:13

browser:                             # All fields migrated from interceptors/browser/config.yaml.
  pipe_timeout_seconds: 5
  fail_behavior: "block"             # "allow" | "block" on pipe error
  temp_dir: ""                       # empty → system %TEMP%
  min_upload_size_bytes: 1024

  domain_blocklist:    [ ...migrated verbatim from the legacy file... ]
  upload_url_keywords: [ ...migrated verbatim... ]
  extensions:          [ ...migrated verbatim... ]
  mime_types:          [ ...migrated verbatim... ]

peripheral_storage:                  # Controller reads this whole subtree.
  target_processes:
    - explorer.exe
  fail_mode: open                    # "open" | "closed"
  shared_memory_name: UsbDlpDriveMap # NOT hot-reloadable (see R3)
  payload_dll_path: Payload.dll      # relative → resolved against Controller.exe dir

  transfer_agent:                    # TransferAgent reads only this nested subtree.
    connect_timeout_ms: 5000         # was hardcoded in OrchestratorClient.cs:24
    analysis_timeout_seconds: 10     # was hardcoded in OrchestratorClient.cs:25
```

**Field migration table** (every legacy field accounted for; nothing dropped):

| Legacy location | New location |
|---|---|
| `interceptors/browser/config.yaml :: pipe_name` | top-level `data_pipe` (deduplicated — same value) |
| `interceptors/browser/config.yaml :: timeout_seconds` | `browser.pipe_timeout_seconds` |
| `interceptors/browser/config.yaml :: fail_behavior/temp_dir/min_upload_size_bytes/extensions/mime_types/domain_blocklist/upload_url_keywords` | `browser.*` (1:1 rename) |
| `Controller/Config/config.yaml :: target_processes/fail_mode/shared_memory_name/payload_dll_path` | `peripheral_storage.*` (1:1 rename) |
| `PipeAgentCore.cs :: timeoutMs=6000` (default) | `clipboard.pipe_timeout_ms` |
| `OrchestratorClient.cs :: ConnectTimeoutMs=5000` | `peripheral_storage.transfer_agent.connect_timeout_ms` |
| `OrchestratorClient.cs :: AnalysisTimeoutS=10` | `peripheral_storage.transfer_agent.analysis_timeout_seconds` |

## Ctl-pipe protocol

The control pipe (ctl-pipe) `\\.\pipe\dlp_agent_ctl` is a duplex, message-mode named pipe — the **same wire shape** as the existing data pipe (`server.py:75` uses `PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT`), so message boundaries come from the pipe itself and no length-prefix framing is needed. One JSON document per pipe message; UTF-8 encoded.

**Lifecycle asymmetry vs. data pipe:** the data pipe opens-and-closes per analysis request. The ctl-pipe is **long-lived** — each subscriber holds one connection for its entire process lifetime so the orchestrator has a handle to push into.

**Wire format:**

```jsonc
// Client → server, sent immediately after connect.
{
  "type": "subscribe",
  "component": "controller" | "clipboard" | "browser",
  "pid": 12345,                     // for orchestrator logging only
  "snapshot_request": true          // see "Race healer" below
}

// Server → client. First response when snapshot_request=true; pushed thereafter on every yaml change.
{
  "type": "config_snapshot" | "config_update",
  "section": "controller" | "clipboard" | "browser",
  "config": { ...the section subtree from config.yaml, plus top-level data_pipe/ctl_pipe... },
  "version": 1738291823             // monotonic int (epoch seconds at parse time);
                                    // clients log it for debugging missed pushes
}

// Server → client, optional. Sent on subscribe failure; the orchestrator closes the connection after sending.
{ "type": "error", "code": "unknown_component" | "parse_failed" | "already_subscribed", "message": "..." }
// "already_subscribed" is retryable (backoff + retry); the others are not.
```

**Race healer** (subscribe-always-snapshots design):
- A subscriber starts up at T=0. It reads `config.yaml` from disk directly to get bootstrap config (this lets it run even if the orchestrator is down).
- The operator edits and saves `config.yaml` at T=1. Orchestrator's config watcher fires and broadcasts to currently-registered subscribers. Our subscriber from T=0 is **not yet registered**, so it misses the push.
- The subscriber connects to the ctl-pipe at T=2 and sends `subscribe` with `snapshot_request:true`. The orchestrator's response is the **current** projection — read fresh from the latest parsed `_raw` config under the orchestrator's config lock. **This wins the race** because the orchestrator's update order is: parse new yaml → update `_raw` (under lock) → broadcast. Any subscribe whose handler runs after that sequence reads the post-update `_raw`.

**Reconnection:** if the subscriber's pipe drops (orchestrator restart, broken pipe), client reconnects with exponential backoff (250 ms → 4 s cap) and re-sends `subscribe` with `snapshot_request:true`. Between disconnect and reconnect the client uses the last-known in-memory config (not a fresh disk read — on-disk + orchestrator-push is the canonical path, not on-disk alone).

**Single instance per component:** the subscriber registry is `dict[str, Handle]` (one handle per component name). On a `subscribe` for a component that already has a live registered handle, the orchestrator responds with `{type:"error", code:"already_subscribed", message:"component=<c> already has a subscriber"}` and closes the new connection. This catches duplicate-launch mistakes loudly (e.g., a developer running two ClipboardInterceptors by accident). **Reconnect case:** when the previous subscriber's pipe drops (process crash or pipe break), the orchestrator's per-handle worker thread sees `ReadFile` return EOF / error, prunes its handle from the registry, and exits. The reconnecting client's subscribe (~250 ms later, after backoff) finds the registry empty for that component and succeeds. There is a brief race window (subscriber crashed but its worker hasn't run its next ReadFile yet) — in that window the new subscribe gets `already_subscribed`. The client treats this as a retryable transient error and backs off; the next attempt succeeds. Phase E will refactor the registry to `dict[(session_id, component), Handle]` when session-aware spawning becomes real.

**Per-write deadline:** each push uses overlapped I/O bounded to **500 ms** so a wedged subscriber cannot block deliveries to other subscribers (see R8).

**ACL:** Phase B uses the **default security descriptor** (`None` to `CreateNamedPipe`), which means same-user access. This matches dev/foreground mode. Phase F will tighten to `BUILTIN\Administrators` only; comment that TODO inline in the ctl_server code so the lift is obvious.

**Server-side threading:** one accept thread; one worker thread per accepted connection (the worker just reads the initial `subscribe`, registers, then blocks on `ReadFile` purely as a connection-death detector). Broadcasts are called synchronously from the file-watch callback, after the watcher's debounce.

## Path discovery (`DLP_CONFIG_PATH`)

Every non-orchestrator process implements the same algorithm:

1. Read env var `DLP_CONFIG_PATH`. If set, validate the file (sentinel check below) and use it. If validation fails, fall through to step 2 with a warning log line.
2. Otherwise, walk up from the discovery anchor (`AppContext.BaseDirectory` in C#; `os.path.dirname(os.path.abspath(__file__))` in Python) looking for a `config.yaml`, **up to N=8 levels**. For each candidate found, apply the sentinel check; if it fails, ignore that candidate and keep walking. N=8 because TransferAgent.exe sits 6 levels below repo root in its standard build path (`interceptors/peripheral_storage/TransferAgent/bin/Debug/net10.0-windows/win-x64/`), and we want headroom for `Release` or a future `publish` subdir.
3. If no candidate passes the sentinel check, write a clear error to stderr/MessageBox naming the env var and every path tried (with the sentinel-failure reason for each), then exit non-zero.

**Sentinel check** (decision #12): a candidate file is accepted only if its parsed YAML root contains a top-level `data_pipe:` key whose value is a non-empty string. This is a fixed ~5-line check in each locator. The check **does not** require any value match — it only confirms the file is a DLP-agent central config (rather than some other tool's `config.yaml` that happened to be in the walk path). Implementation: in C#, deserialize to `Dictionary<object,object>` and check `dict.ContainsKey("data_pipe") && dict["data_pipe"] is string s && !string.IsNullOrEmpty(s)`; in Python, `yaml.safe_load(...)` + `isinstance(d.get("data_pipe"), str) and d["data_pipe"]`. Errors during parse (malformed YAML) count as sentinel failure — the locator keeps walking.

mitmproxy addon also accepts `--set dlp_config_path=<path>` (added via `loader.add_option(...)` in `addon.py`'s `load(loader)` hook). When set, it takes precedence over the env var.

**ShellExtension-launched TransferAgent**: ShellExtension is a COM DLL hosted in `explorer.exe`. Explorer inherits the user's environment, so an `DLP_CONFIG_PATH` set in user-env vars (manually for dev, or by the Phase D installer's machine-env-var write + `WM_SETTINGCHANGE` broadcast) is visible to TransferAgent. The N=8 walk-up is the fallback for fresh dev machines.

## Implementation tasks

Ordered so that the codebase compiles and runs at every step.

### IT-B1. Rename + Schema + `orchestrator/config.py` + harness yaml shape

**Goal:** the central config file is renamed and the new sectioned yaml exists and is parsed; orchestrator-internal behavior is unchanged.

- **First step:** `git mv orchestrator.yaml config.yaml` at repo root (decision #12). This preserves git history on the file.
- Rewrite `D:\Code\GithubPublishEndpointDLP\config.yaml` to the schema above. Copy all current `browser/config.yaml` and `Controller/Config/config.yaml` field values verbatim into the new sections (no behavior change, only relocation).
- `orchestrator/config.py`: update the default-path constant from `orchestrator.yaml` to `config.yaml`. Update `--config` help text in `orchestrator/__main__.py` accordingly.
- `orchestrator/config.py`: keep the flat `OrchestratorConfig` dataclass as-is (so `server.py`/`dispatcher.py`/`policy_manager.py` need zero changes). Add a single new attribute `_raw: dict` populated in `load_config()` holding the whole parsed yaml. The ctl-pipe server reads `_raw` to project sections; nothing else does. Reuse: `load_config(path)` already accepts an optional path (Phase A IT0), already uses `yaml.safe_load`.
- `scripts/harness/conftest.py`: extend the `config` dict (lines 124-142) with the three new sections (`clipboard:`, `browser:`, `peripheral_storage:` with nested `transfer_agent:`). Minimal values — tests don't need full lists. The existing `ctl_pipe_name` allocation (line 111) already plumbs `ctl_pipe` per-test, no change.

**Validates:** existing Phase A pytests still pass. `python -m orchestrator --foreground --config config.yaml` still starts.

### IT-B2. New `orchestrator/ctl_server.py` + `orchestrator/config_watcher.py`

**Goal:** ctl-pipe accepts subscribes; FileSystemWatcher on `config.yaml` triggers broadcasts.

- New `orchestrator/ctl_server.py` (~250 LOC):
  - Class `CtlServer(config, raw_provider: Callable[[], dict])`. `raw_provider` returns the latest `_raw` (closure over a mutable cell maintained by `__main__`).
  - `run()` opens the ctl-pipe via `win32pipe.CreateNamedPipe` with the same flags as `server.py:72-81`, then `ConnectNamedPipe`, spawning a worker thread per accepted client. Mirror `PipeServer.stop()`'s throwaway-connect unblock pattern.
  - Worker: reads one `subscribe` JSON, validates `component`. Attempts to register handle in `_SubscriberRegistry` (lock-guarded `dict[str, Handle]`). If the component already has a live entry → write `{type:"error", code:"already_subscribed", ...}`, close the connection, worker exits. Otherwise → register the handle, respond with `config_snapshot` if requested (project from `raw_provider()`), then enter `ReadFile` loop as connection-death detector. **On EOF/error from ReadFile**: the worker prunes its OWN handle from the registry (under the registry lock) before exiting — this is how reconnect cases self-resolve.
  - `broadcast(component_name)`: looks up the (at most one) handle for that component; writes `config_update` JSON via overlapped I/O bounded at 500 ms (see R8); if the write fails, the worker thread will detect it on its next read and clean itself up — broadcast itself just logs the failure.
- New `orchestrator/config_watcher.py` (~80 LOC):
  - Class `ConfigWatcher(yaml_path, on_change)`. Uses `watchdog` (already a dep in PolicyManager). Mirrors `_ReloadHandler` from `policy_manager.py:22-51` — handles `on_modified`, `on_moved` (atomic save), `on_created` (delete-and-recreate editors). Debounce 200 ms.
  - On change: re-read + parse yaml. On parse error: log and keep old `_raw`. On success: call `on_change(new_raw)`.
- `orchestrator/__main__.py` (`_run_foreground`):
  - Build a mutable `_raw` cell.
  - Build `CtlServer(config, lambda: _raw_cell["raw"])` and `ConfigWatcher(args.config or default_path, on_change=_handle_change)`.
  - `_handle_change(new_raw)` implements decision #7's selective-skip:
    1. Compute `pipe_field_changed = (new_raw["data_pipe"] != _raw_cell["raw"]["data_pipe"]) or (new_raw["ctl_pipe"] != _raw_cell["raw"]["ctl_pipe"])`.
    2. If pipe fields changed, log `"data_pipe/ctl_pipe change requires restart; keeping <old>"` (one log line per affected field), then **override the new yaml's pipe-name fields back to the old values** in a copy. The override copy is what gets stored in `_raw_cell["raw"]` and what `CtlServer.broadcast()` projects from.
    3. Always call `ctl_server.broadcast()` for each component — even if pipe fields were the *only* fields that changed (the broadcast is a no-op in that case because the projection equals the pre-update snapshot, but the iteration is harmless and keeps the code path uniform).
  - Start `CtlServer.run()` on a daemon thread named `ctl-server`; start the watchdog Observer.
  - Ctrl+C teardown: stop ctl_server (throwaway connect to break accept), stop watcher Observer, in addition to existing PipeServer/Dispatcher/PolicyManager shutdown.
- `orchestrator/policy_manager.py`: **no code change**. The existing observer watches `analyzer/` (line 68: `Path(self._policies_file).resolve().parent`); the new watcher watches the repo root. Disjoint directories. Add a one-line comment near the existing `.schedule(...)` call noting that the filename filter in `_ReloadHandler` makes co-location safe if these ever share a dir (defensive doc only).

**New test** at `scripts/harness/test_ctl_pipe.py`: two cases — both use a tiny in-test pywin32 ctl-pipe client (~40 LOC, mirroring `scripts/harness/pipe_helpers.py`).

(a) **`test_subscribe_returns_snapshot`** — subscribe with `snapshot_request:true`; assert the orchestrator returns a `config_snapshot` whose `config` subtree matches the spawned orchestrator's `config.yaml` (compare against `yaml.safe_load(orch.config_path.read_text())`).

(b) **`test_yaml_save_selective_skip_and_propagate`** — exercises decision #7's selective-skip end-to-end. Steps:
  1. Subscribe as `"browser"`, drain the initial `config_snapshot`, record `data_pipe = old_pipe` and `pipe_timeout_seconds = old_timeout` and `fail_behavior = old_behavior`.
  2. Subscribe a second ctl-pipe client as `"clipboard"`, record `clipboard.pipe_timeout_ms = old_clipboard_timeout`.
  3. Atomically rewrite the orchestrator's yaml so that **three fields change in the same save**:
     - `data_pipe` → some new pipe name (non-hot-reloadable)
     - `browser.fail_behavior` → flipped value (hot-reloadable)
     - `clipboard.pipe_timeout_ms` → a new value, e.g. `old_clipboard_timeout + 1000` (hot-reloadable)
  4. Within 1.5 s, assert the orchestrator log file contains `data_pipe change requires restart; keeping <old_pipe>` exactly once.
  5. Within 1.5 s, assert both subscribers receive exactly one `config_update`. Inspect them:
     - The `"browser"` payload's `data_pipe` equals `old_pipe` (NOT the new value); `fail_behavior` equals the new flipped value.
     - The `"clipboard"` payload's `data_pipe` equals `old_pipe`; `pipe_timeout_ms` equals `old_clipboard_timeout + 1000`.
  6. Assert no second `config_update` arrives in the next 500 ms (no double-fire).

This proves (i) the broadcast still fires when a non-hot-reloadable field is in the change-set, (ii) only the hot-reloadable fields actually propagate, and (iii) the pipe-name override is applied uniformly across subscribers.

**Validates:** new test passes; orchestrator log shows `Ctl pipe listening on \\.\pipe\dlp_agent_ctl` and `Config watcher watching <abs>`.

### IT-B3. New shared C# library `src/DlpShared/`

**Goal:** path discovery and ctl-pipe subscription, available as a project reference.

- New project `src/DlpShared/DlpShared.csproj` targeting `net10.0-windows`. Add `<PackageReference Include="YamlDotNet" Version="17.0.1" />` and `<PackageReference Include="System.IO.Pipes" />` (already in BCL but list explicitly for clarity).
- New `src/DlpShared/ConfigLocator.cs`:
  - `public static string FindConfigYaml(string? anchorOverride = null)` — implements the discovery algorithm (env var → walk-up N=8 with sentinel check → throw with a clear message naming the env var and every path tried, including the sentinel-failure reason for each candidate). The `anchorOverride` parameter is a test seam (default `null` → use `AppContext.BaseDirectory`); see `ConfigLocatorTests` in the *Validates* note below. Named after the central file (`config.yaml`) so the function name tracks the file name decision (#12).
  - `private static bool HasDataPipeSentinel(string yamlPath)` — parses the file, returns true iff the YAML root has a non-empty string `data_pipe` key. Catches YAML parse errors and returns false. Used by `FindConfigYaml`.
  - `public static T LoadSection<T>(string yamlPath, string sectionKey)` — deserializes the file as a typed container using YamlDotNet with `UnderscoredNamingConvention.Instance` (matches Controller's existing convention from AppConfig.cs), then plucks `sectionKey` and re-deserializes into `T`. For top-level fields (`data_pipe`, `ctl_pipe`), expose `LoadTopLevel(string yamlPath) → (string dataPipe, string ctlPipe)`.
- New `src/DlpShared/CtlPipeSubscriber.cs` (~150 LOC):
  - `class CtlPipeSubscriber(string ctlPipeName, string componentName, Action<JsonElement> onConfigChanged)`.
  - `StartAsync(CancellationToken)`: connects via `NamedPipeClientStream` with `PipeOptions.Asynchronous`. Sets `ReadMode = PipeTransmissionMode.Message` (mirrors `OrchestratorClient.cs:61`). Sends `subscribe` with `snapshot_request:true`. Loops on read; for each `config_snapshot` or `config_update`, invokes `onConfigChanged(jsonElement.GetProperty("config"))`.
  - On receipt of `{type:"error", code:"already_subscribed"}`: log a warning, close the connection, exponential-backoff retry (250 ms → 4 s cap). This handles the brief reconnect race documented in the *Single instance per component* paragraph above.
  - On receipt of any other `error` code (`unknown_component`, `parse_failed`): log error and exit — these are not retryable bugs in the client.
  - On `IOException` / `EndOfStreamException`: exponential backoff (250 ms → 4 s cap) then reconnect + re-subscribe. `OperationCanceledException` exits cleanly.
- `src/AgentCore/AgentCore.csproj`: add `<ProjectReference Include="..\DlpShared\DlpShared.csproj" />`.

**Validates:** `dotnet build src\DlpShared\DlpShared.csproj`. Plus a new test class `src/AgentCore.Tests/ConfigLocatorTests.cs` with five cases (each builds a temp directory tree under `Path.GetTempPath()`, sets `Environment.SetEnvironmentVariable("DLP_CONFIG_PATH", ...)` per-case, and uses `AppDomain.CurrentDomain.BaseDirectory` overrides via a test seam — pass the anchor directory as an optional parameter to `FindConfigYaml(anchorOverride: ...)` rather than mocking AppContext):

  1. **`EnvVarPointsAtValidFile_ReturnsThatPath`** — write a temp yaml containing `data_pipe: "x"`, set `DLP_CONFIG_PATH` to its absolute path, call `FindConfigYaml()`; assert returned path equals the env-var value, exact string match.
  2. **`EnvVarPointsAtFileMissingSentinel_FallsThroughToWalkUp`** — write a temp yaml WITHOUT a `data_pipe` key, set `DLP_CONFIG_PATH` to it. Also place a valid yaml (with sentinel) 2 levels up from the test anchor. Assert `FindConfigYaml(anchor)` returns the walk-up file, AND the log captures a warning that the env-var-pointed file failed the sentinel check.
  3. **`WalkUpFindsValidConfigAtDepth4`** — unset env var, build an anchor 4 levels below a temp directory containing a sentinel-valid yaml. Assert `FindConfigYaml(anchor)` returns the depth-4 path.
  4. **`WalkUpSkipsMisleadingConfigWithoutSentinel`** — unset env var. Place a "misleading" `config.yaml` (no `data_pipe` key) at depth 2 above the anchor, and a valid one at depth 5. Assert `FindConfigYaml(anchor)` returns the depth-5 path (proving the sentinel skips the wrong file and walking continues), AND the misleading path appears in the post-test log of attempted candidates.
  5. **`NoEnvVarAndNoFileFound_ThrowsWithDiagnostics`** — unset env var, anchor inside a fresh empty temp directory with no yaml anywhere up the chain. Assert `FindConfigYaml(anchor)` throws (use `FileNotFoundException` or a custom `ConfigNotFoundException` — pick one in IT-B3). The exception message must contain (a) the string `DLP_CONFIG_PATH`, (b) every absolute path that was checked.

Test-seam note: do NOT mock `AppContext.BaseDirectory`. Instead, give `FindConfigYaml` an internal-visible optional `string anchorOverride` parameter defaulting to `AppContext.BaseDirectory`; tests pass the temp anchor. Production callers (Controller, ClipboardInterceptor, TransferAgent) use the default.

### IT-B4. `PipeAgentCore` provider overload + ClipboardInterceptor migration

**Goal:** ClipboardInterceptor reads config.yaml, subscribes to ctl-pipe, and lives with hot-reloadable `pipe_timeout_ms`.

- `src/AgentCore/PipeAgentCore.cs`:
  - Add new **constructor** overload (referred to as a "ctor" in places below for brevity — same thing): `public PipeAgentCore(Func<(string pipeName, int timeoutMs)> provider)`.
  - Refactor `_pipeName` / `_timeoutMs` access through a private accessor that either returns the constants (legacy constructor) or calls the provider (new constructor). The provider is called **once per AnalyseAsync** invocation, not per I/O — calling per-I/O would risk the connect and read seeing different pipe-name/timeout values mid-transaction.
  - Keep the existing default-arg constructor unchanged so `src/AgentCore.Tests/AgentCoreTests.cs` (Phase A's two cases) keeps compiling.
- `src/ClipboardInterceptor/Program.cs`:
  - At startup, call `DlpShared.ConfigLocator.FindConfigYaml()`; extract `data_pipe`, `ctl_pipe`, and `clipboard.pipe_timeout_ms` via `ConfigLocator.LoadSection`.
  - Build a small thread-safe `ConfigHolder` holding current `(pipeName, timeoutMs)`; `Volatile`/`Interlocked` are sufficient — only ever read-or-write whole pair atomically.
  - `var agentCore = new PipeAgentCore(() => holder.Current);` (provider form).
  - Start `var subscriber = new CtlPipeSubscriber(ctlPipe, "clipboard", json => holder.ApplyUpdate(json));` on a `Task.Run`. `ApplyUpdate` reads `pipe_timeout_ms` from the JSON and updates the holder. Reads `data_pipe` from the JSON only for warning (R3): if it differs from in-use, log `data_pipe change requires restart; keeping <old>`.
- `src/ClipboardInterceptor/ClipboardInterceptor.csproj`: no change — already references `AgentCore`, so the new transitive `DlpShared` dep flows in.

**Validates:** build ClipboardInterceptor, run alongside the orchestrator, copy text. Verify the **data-pipe** round-trip works — meaning: ClipboardInterceptor sends the copied text payload over the `data_pipe` (`\\.\pipe\dlp_agent`) to the orchestrator, the orchestrator analyzes it, and the ALLOW/BLOCK decision flows back over the same `data_pipe` connection. This is the existing Phase A behavior; we are only confirming it still works after the config-source refactor (the `data_pipe` name and `pipe_timeout_ms` now come from yaml + ctl-pipe holder, not constants). Separately, the **ctl-pipe** subscription is a one-way long-lived push channel — confirm it works by editing `clipboard.pipe_timeout_ms` in yaml and watching ClipboardInterceptor log a `ctl: pipe_timeout_ms updated → <new>` line.

### IT-B5. Browser addon migration

**Goal:** addon reads config.yaml's `browser` section, subscribes to ctl-pipe in a background thread.

- `interceptors/browser/config.py`:
  - `Config` dataclass shape unchanged (so all `addon.py` field accesses keep compiling).
  - Rewrite `load_config(orchestrator_yaml_path)` to read the whole file, pull `data_pipe` + the `browser` subtree, and return a populated `Config`. Drop the wrong-by-default `r"\\.\pipe\dlp_upload"` fallback in the `Config` dataclass.
  - Add `find_config_yaml() -> str` — Python mirror of `DlpShared.ConfigLocator.FindConfigYaml()` (env var, walk-up N=8, clear error).
- `interceptors/browser/addon.py`:
  - In `load(loader)` (line 107): register the mitmproxy option with `loader.add_option("dlp_config_path", str, "", "Path to config.yaml")`. Resolve config path order: mitmproxy option (`from mitmproxy import ctx; ctx.options.dlp_config_path`) → env var `DLP_CONFIG_PATH` → `find_config_yaml()`.
  - Call `load_config(resolved_path)` instead of the hardcoded local `config.yaml` (which is being deleted in this task).
  - Add a module-level `_cfg_lock = threading.Lock()` and wrap reads/writes of `_cfg` (most accesses are single-field-read, the lock is mainly to protect against partial-config visibility during the swap — see R4).
  - Start a daemon thread running a new `CtlPipeSubscriber` (Python version, IT-B6 below). On each `config_snapshot` / `config_update`, build a fresh `Config` from the payload, then `with _cfg_lock: _cfg = new_cfg`. Warn-and-ignore on data_pipe / ctl_pipe change (R3).
- `interceptors/browser/pipe_client.py`: no change. Already takes pipe name + timeout as arguments.
- Delete `interceptors/browser/config.yaml`.

### IT-B6. New Python ctl-pipe subscriber `interceptors/browser/ctl_pipe_subscriber.py`

**Goal:** mirror of `DlpShared.CtlPipeSubscriber.cs` for the addon.

~120 LOC. Uses pywin32 (already a dep — `interceptors/browser/pipe_client.py` uses it). Class `CtlPipeSubscriber(pipe_name, component_name, on_change)`; `start()` spawns a daemon thread; loop is: `win32pipe.WaitNamedPipe` → `win32file.CreateFile` → `win32pipe.SetNamedPipeHandleState(PIPE_READMODE_MESSAGE)` → write `subscribe` JSON → loop `win32file.ReadFile` dispatching JSONs to `on_change`. On `pywintypes.error`: close handle, sleep with exponential backoff (250 ms → 4 s), reconnect. On `{type:"error", code:"already_subscribed"}` JSON: log warning, close, retry with backoff (same handling as the C# subscriber). On any other `error` code: log error and exit the subscriber thread.

**Validates (combined IT-B5+IT-B6):** `mitmdump -s interceptors/browser/addon.py --set dlp_config_path=<repo>/config.yaml`. Addon log line at addon.py:115 reflects the `config.yaml` values.

Then, mirroring IT-B2 test case (b), exercise the selective-skip end-to-end at the addon level:

1. Record the addon's in-use `data_pipe`, `browser.fail_behavior`, and `browser.pipe_timeout_seconds`.
2. Atomically save the yaml with **three** simultaneous edits:
   - `data_pipe` → a new pipe name (non-hot-reloadable).
   - `browser.fail_behavior` → flipped value (hot-reloadable).
   - `browser.pipe_timeout_seconds` → a new value (hot-reloadable).
3. Within ~1 s, observe addon log:
   - `ctl: config_update received; fail_behavior=<new>; pipe_timeout_seconds=<new>; data_pipe=<old, kept>`. (Note the log line names all three to make selective-skip visible.)
   - A warning: `data_pipe change requires restart; keeping <old>`.
4. Trigger a deliberate data-pipe timeout (kill orchestrator briefly). Verify addon now applies the NEW `fail_behavior` (e.g., allows instead of blocks if flipped from `block`→`allow`), proving the hot-reloadable change took effect.
5. Verify the addon is still using the OLD `data_pipe` (it still connects to the original pipe name; the orchestrator's pipe server is also still bound there), proving the non-hot-reloadable field did NOT take effect.

This confirms the addon's local `_cfg` swap honors the orchestrator's pre-broadcast override of the unchangeable fields.

### IT-B7. TransferAgent migration

**Goal:** TransferAgent reads config.yaml once at startup, no ctl-pipe.

- `interceptors/peripheral_storage/TransferAgent/DlpTransferAgent.csproj`: add `<ProjectReference Include="..\..\..\src\DlpShared\DlpShared.csproj" />`. (No YamlDotNet PackageReference needed — flows in transitively.)
- `interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs`:
  - Lines 23-25: delete the three `const` declarations.
  - Add `internal static string PipeName = "dlp_agent";`, `internal static int ConnectTimeoutMs = 5000;`, `internal static int AnalysisTimeoutS = 10;` as defaults (overwritten by `LoadConfig`).
  - Add `internal static void LoadConfig()` — uses `DlpShared.ConfigLocator.FindConfigYaml()` + `LoadTopLevel(...)` + `LoadSection<TransferAgentConfig>(path, "peripheral_storage")` (where TransferAgentConfig contains the `transfer_agent` subobject) and assigns the three statics.
  - `AnalyzeAsync` body: only change is references to the static fields.
- `interceptors/peripheral_storage/TransferAgent/Program.cs`: call `OrchestratorClient.LoadConfig()` after argv parsing and before `Application.Run`. On `Exception`: MessageBox with the error and exit (same UX as the existing "destination folder does not exist" branch).

**Validates:** build TransferAgent, trigger from ShellExtension context menu, transfer a file. Add a startup log line via `OutputDebugString` or a temp audit file: `loaded orchestrator config from <path>; pipe=<name> timeouts={connect=Xms analysis=Ys}`.

### IT-B8. Controller migration

**Goal:** Controller reads config.yaml directly; ctl-pipe subscriber replaces FileSystemWatcher.

- `interceptors/peripheral_storage/Controller/Controller.csproj`: add `<ProjectReference Include="..\..\..\src\DlpShared\DlpShared.csproj" />`. (YamlDotNet 17.0.1 already declared in the csproj — leave it; DlpShared brings its own copy of the same version.)
- `interceptors/peripheral_storage/Controller/Config/AppConfig.cs`:
  - Keep the existing property tree (`TargetProcesses`, `FailMode`, `SharedMemoryName`, `PayloadDllPath`, `FailClosed` computed) — same shape.
  - The class now represents the `peripheral_storage:` section (minus the nested `transfer_agent:` subobject, which Controller ignores). The plucking happens in `DlpShared.ConfigLocator.LoadSection<AppConfig>(path, "peripheral_storage")` — YamlDotNet ignores extra keys by default, so `transfer_agent` is simply skipped during deserialization.
- `interceptors/peripheral_storage/Controller/Program.cs`:
  - Lines 12-22 (load config): replace with `var yamlPath = DlpShared.ConfigLocator.FindConfigYaml(); var (dataPipe, ctlPipe) = DlpShared.ConfigLocator.LoadTopLevel(yamlPath); var config = DlpShared.ConfigLocator.LoadSection<AppConfig>(yamlPath, "peripheral_storage");`.
  - Lines 64-89 (`ExportRunningConfig`): keep — it's a useful audit. Update the header comment to say the source is now `config.yaml`'s `peripheral_storage` section.
  - Lines 91-167 (`TryReload` + `FileSystemWatcher` + `debounceTimer`): **delete the watcher/timer**. Keep `TryReload`'s body — extract it into a method that takes a new `AppConfig` directly. The existing selective updates (`ProcessMonitor.UpdateTargets`, `SharedMemoryWriter.UpdateFailClosed`, `Injector.UpdateDllPath`, `shared_memory_name` rejection at 120-128) are preserved; only the trigger source changes.
  - Replace the watcher with `var subscriber = new DlpShared.CtlPipeSubscriber(ctlPipe, "controller", OnConfigPush); _ = Task.Run(() => subscriber.StartAsync(cts.Token), cts.Token);`. `OnConfigPush` deserializes the `JsonElement` payload (System.Text.Json, snake_case naming policy — same convention as `OrchestratorClient.cs`) into a new `AppConfig`, then calls the extracted `TryReload(newConfig)`.
  - Existing `aliveMutex.Dispose()` ordering at lines 184-186 unchanged.
- Delete `interceptors/peripheral_storage/Controller/Config/config.yaml`.

**Validates:** build Controller, run it. Verify `ctl: subscribed; snapshot version=...` line. Edit `peripheral_storage.fail_mode` in config.yaml; observe Controller's existing log line at Program.cs:139 (`fail_mode updated: closed`) fire from the ctl-driven `TryReload` path. Verify `running-config.yaml` updates.

### IT-B9. Update `integration-plan2.md`

**Goal:** future planning sessions see the new state, not the pre-Phase-B state. (Locked decision #11.)

- **Rename:** find/replace every prose mention of `orchestrator.yaml` → `config.yaml` throughout the file. There are roughly half a dozen such mentions (Context, Locked decisions, Phase B body, Critical files, Phase D body).
- In **Current state snapshot** ("Implemented and roughly working"), add: *Unified `config.yaml` (renamed from `orchestrator.yaml`) is the source of truth for non-policy config. Ctl-pipe push-based hot reload (`orchestrator/ctl_server.py` + `orchestrator/config_watcher.py`) covers Controller, ClipboardInterceptor, and the browser addon; TransferAgent does a one-shot read.*
- Phase B body (lines 75-86 in integration-plan2.md): replace the "Scope of follow-up planning session" bullets with **Completed.** plus a one-paragraph outcome summary that names the locked decisions (Q1=B, Q2=B, Q3=1, Q4=2, D1=b, D2=a, D3=a, plus the rename + sentinel-check from decision #12).
- **Critical files** (lines 51-57): add `orchestrator/ctl_server.py`, `orchestrator/config_watcher.py`, `src/DlpShared/ConfigLocator.cs`, `src/DlpShared/CtlPipeSubscriber.cs`, `interceptors/browser/ctl_pipe_subscriber.py`. Remove the entry for `Controller/Config/config.yaml` (deleted). Rename the `orchestrator.yaml` entry to `config.yaml`.
- **Open questions** (lines 149-160): strike #1 and #2 (resolved). Leave #3–#8 untouched (they belong to later phases).

## Critical files

**Code edits**
- `D:\Code\GithubPublishEndpointDLP\config.yaml` — full schema rewrite (IT-B1)
- `D:\Code\GithubPublishEndpointDLP\orchestrator\config.py` — add `_raw` (IT-B1)
- `D:\Code\GithubPublishEndpointDLP\orchestrator\__main__.py` — wire CtlServer + ConfigWatcher into `_run_foreground` + shutdown (IT-B2)
- `D:\Code\GithubPublishEndpointDLP\orchestrator\policy_manager.py` — one-line doc comment only (IT-B2)
- `D:\Code\GithubPublishEndpointDLP\scripts\harness\conftest.py` — add new sections to test config (IT-B1)
- `D:\Code\GithubPublishEndpointDLP\src\AgentCore\PipeAgentCore.cs` — provider constructor overload (IT-B4)
- `D:\Code\GithubPublishEndpointDLP\src\AgentCore\AgentCore.csproj` — ProjectReference to DlpShared (IT-B3)
- `D:\Code\GithubPublishEndpointDLP\src\ClipboardInterceptor\Program.cs` — yaml load + subscribe (IT-B4)
- `D:\Code\GithubPublishEndpointDLP\interceptors\browser\addon.py` — yaml load + mitmproxy option + subscribe thread (IT-B5)
- `D:\Code\GithubPublishEndpointDLP\interceptors\browser\config.py` — rewrite `load_config`, add `find_config_yaml` (IT-B5)
- `D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\Controller\Program.cs` — remove FileSystemWatcher, add subscriber, extract TryReload (IT-B8)
- `D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\Controller\Config\AppConfig.cs` — header comment + verify ignore-extra-keys behavior (IT-B8)
- `D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\Controller\Controller.csproj` — ProjectReference to DlpShared (IT-B8)
- `D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\TransferAgent\OrchestratorClient.cs` — replace consts with `LoadConfig` statics (IT-B7)
- `D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\TransferAgent\Program.cs` — call `LoadConfig` at startup (IT-B7)
- `D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\TransferAgent\DlpTransferAgent.csproj` — ProjectReference to DlpShared (IT-B7)
- `D:\Code\GithubPublishEndpointDLP\integration-plan2.md` — Phase B completion edits (IT-B9)

**New files**
- `D:\Code\GithubPublishEndpointDLP\orchestrator\ctl_server.py` (IT-B2)
- `D:\Code\GithubPublishEndpointDLP\orchestrator\config_watcher.py` (IT-B2)
- `D:\Code\GithubPublishEndpointDLP\src\DlpShared\DlpShared.csproj` (IT-B3)
- `D:\Code\GithubPublishEndpointDLP\src\DlpShared\ConfigLocator.cs` (IT-B3)
- `D:\Code\GithubPublishEndpointDLP\src\DlpShared\CtlPipeSubscriber.cs` (IT-B3)
- `D:\Code\GithubPublishEndpointDLP\interceptors\browser\ctl_pipe_subscriber.py` (IT-B6)
- `D:\Code\GithubPublishEndpointDLP\scripts\harness\test_ctl_pipe.py` (IT-B2)

**Deleted files**
- `D:\Code\GithubPublishEndpointDLP\interceptors\browser\config.yaml` (IT-B5)
- `D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\Controller\Config\config.yaml` (IT-B8)

**Reused (no edits, important for context)**
- `orchestrator/server.py:72-81` — message-mode pipe creation; ctl_server matches its flags exactly
- `orchestrator/policy_manager.py:22-51` — `_ReloadHandler` pattern (on_modified + on_moved + on_created + debounce); `ConfigWatcher` mirrors this shape
- `scripts/harness/conftest.py:111,126` — already plumbs `ctl_pipe_name` per test
- `interceptors/peripheral_storage/Controller/Program.cs:93-154` — `TryReload`'s selective-field-update logic is preserved verbatim, only its trigger changes from FileSystemWatcher to ctl-push
- `interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs:61` — message-mode read; CtlPipeSubscriber.cs uses the same pattern
- YamlDotNet 17.0.1 (`Controller.csproj`) — verified current stable; `UnderscoredNamingConvention` matches snake_case yaml keys to PascalCase C# properties

## Verification

### Build commands (verified to be the correct invocations)

From repo root in **Visual Studio 2026 Developer PowerShell**:

```powershell
dotnet build src\DlpShared\DlpShared.csproj
dotnet build src\AgentCore\AgentCore.csproj
dotnet build src\ClipboardInterceptor\ClipboardInterceptor.csproj
dotnet build interceptors\peripheral_storage\Controller\Controller.csproj
dotnet build interceptors\peripheral_storage\TransferAgent\DlpTransferAgent.csproj
dotnet test src\AgentCore.Tests\AgentCore.Tests.csproj
```

From **normal PowerShell**:

```powershell
python -m pytest scripts\harness\ -v
python -m orchestrator --foreground --config config.yaml
mitmdump -s interceptors\browser\addon.py --listen-port 8080 --set dlp_config_path=$pwd\config.yaml
```

### Per-component smoke (foreground dev mode)

1. **Orchestrator startup** — confirm new log lines: `Pipe server listening on \\.\pipe\dlp_agent (4 accept threads)` (existing), `Ctl pipe listening on \\.\pipe\dlp_agent_ctl` (new), `Config watcher watching <abs path>` (new).
2. **ClipboardInterceptor** — start, copy text. Confirm log lines: `Loaded orchestrator config: <path>`, `Subscribing to ctl pipe: \\.\pipe\dlp_agent_ctl`, `ctl: snapshot received`. Pipe round-trip succeeds.
3. **Browser addon** — start mitmdump. addon.py:115 log reflects config.yaml values (not the deleted local config). Confirm `ctl: subscribed; snapshot version=...`.
4. **Controller** — start. Existing log lines at Program.cs:30-32 reflect config.yaml. Confirm `ctl: subscribed; snapshot version=...`.
5. **TransferAgent** — trigger via ShellExtension context menu. Startup log line shows the configured pipe + timeouts. Transfer succeeds.

### Hot-reload propagation

- Orchestrator + ClipboardInterceptor + Controller + (mitmdump+addon) all running.
- Edit `config.yaml`: change `browser.fail_behavior` from `block` to `allow`, save (atomic — most editors).
- Orchestrator log: `Config changed, parsing... broadcasting to N subscribers (controller=1, clipboard=1, browser=1)`.
- Addon log: `ctl: config_update received; fail_behavior=allow`.
- Kill orchestrator briefly to force a pipe timeout in the addon; verify addon now `ALLOW`s instead of `BLOCK`s.
- Re-test with `peripheral_storage.fail_mode` flip (Controller path) and `clipboard.pipe_timeout_ms` flip (ClipboardInterceptor path).

### Pipe-name non-hot-reload semantics (selective-skip)

- **Case 1 — pipe name alone changes.** Edit `config.yaml` to change only `data_pipe`. Orchestrator log: `data_pipe change requires restart; keeping <old value>`. Broadcasts still fire to each component, but the payload's `data_pipe` field equals the old in-use value (no actual change reaches subscribers). Clients still operating on the old name continue to work.
- **Case 2 — pipe name AND other fields change in the same save.** Edit `config.yaml` to change both `data_pipe` AND `browser.fail_behavior`. Orchestrator log: `data_pipe change requires restart; keeping <old>` followed by the normal `broadcasting to N subscribers ...`. Addon log shows `config_update received; fail_behavior=<new>; data_pipe=<old, unchanged>`. The new fail_behavior takes effect; the pipe name does not.
- **Case 3 — duplicate-launch detection (decision #13).** Start one ClipboardInterceptor; verify `ctl: subscribed`. Start a second one. Second one's log: `ctl: already_subscribed (component=clipboard); backing off`. Stop the first; within ~250 ms the second one's retry succeeds and logs `ctl: subscribed`.

### Automated tests

- `pytest scripts/harness/ -v` — all Phase A tests + the new `test_ctl_pipe.py` (two cases, including the selective-skip end-to-end in case (b)) pass.
- `dotnet test src\AgentCore.Tests\AgentCore.Tests.csproj` — Phase A's two `PipeAgentCore` cases still pass (they use the literal-value constructor), plus the five new `ConfigLocatorTests` cases specified in IT-B3 (env-var-valid, env-var-fails-sentinel, walk-up-finds, walk-up-skips-misleading, none-found-throws).

### Documentation edit

- Confirm `integration-plan2.md` reflects the new state (Phase B body marked complete, Current State Snapshot extended, Open Questions 1 & 2 struck).

## Risks and follow-ups

**R1 — Disk-read-then-subscribe race.** *Scenario:* client disk-reads yaml at T=0; operator edits + saves at T=1; client subscribes at T=2 → registered set didn't include this client at T=1, so the broadcast missed it. *Mitigation:* every `subscribe` carries `snapshot_request: true`. Orchestrator's update order is parse → update `_raw` under lock → broadcast; any subscribe whose handler runs after that sequence reads post-update `_raw`. See *Ctl-pipe protocol → Race healer*.

**R2 — Two file watchers on overlapping content.** Already analyzed: PolicyManager watches `analyzer/` (line 68), ConfigWatcher watches repo root. Disjoint. Even if a future phase co-locates the files, the filename filter in `_ReloadHandler` (line 29) prevents cross-fire. No code change required.

**R3 — `data_pipe` / `ctl_pipe` hot-reload is unsafe.** Changing pipe names at runtime would partition orchestrator from existing clients. *Decision (locked):* the two fields are individually non-hot-reloadable. When `config.yaml` changes them alongside other fields, the orchestrator logs `data_pipe/ctl_pipe change requires restart; keeping <old value>` and **continues broadcasting the other field changes** with the unchangeable fields overridden back to the in-use values (so subscribers see an internally-consistent snapshot). Subscribers also warn-and-ignore on receive if they ever observe a divergent pipe name. Same selective-field-rejection pattern as Controller's existing `shared_memory_name` rejection (Program.cs:120-128).

**R4 — mitmproxy single-threaded addon hook + Python subscriber thread.** mitmproxy hooks run on the main thread; a `threading.Thread` for the subscriber is the same shape as existing code at `addon.py:1008` (`_notify_blocked`). The shared mutable `_cfg` is protected by `_cfg_lock` (added in IT-B5). Subscriber thread builds a new `Config` from the push payload, then `with _cfg_lock: _cfg = new_cfg` — atomic from any other thread's perspective.

**R5 — ClipboardInterceptor pipe re-bind.** `data_pipe` non-hot-reload (R3) eliminates this. `pipe_timeout_ms` is the only hot field, and `PipeAgentCore` reads it via the provider per `AnalyseAsync` call — no in-flight teardown needed.

**R6 — ShellExtension → TransferAgent env inheritance.** ShellExtension is hosted in `explorer.exe`. Explorer inherits user-env vars. **Dev:** the developer sets `DLP_CONFIG_PATH` as a user env var (`setx DLP_CONFIG_PATH <path>`) and restarts explorer.exe via Task Manager → "Restart". **Phase D installer:** writes `HKLM\System\CurrentControlSet\Control\Session Manager\Environment\DLP_CONFIG_PATH` and broadcasts `WM_SETTINGCHANGE`. *Phase B doc requirement:* mention this in a developer note in the implementer's PR body. As a fallback, the N=8 walk-up (decision #10) covers TransferAgent.exe's standard build path.

**R7 — Build path walk-up depth.** TransferAgent.exe lives 6 levels below repo root in `interceptors/peripheral_storage/TransferAgent/bin/Debug/net10.0-windows/win-x64/`. N=8 gives 2-level headroom for `Release` configs and a future `publish` subdir. Cheap loop, no harm.

**R8 — Broadcast write-bound on a wedged subscriber.** If one subscriber's process is suspended or hung, `WriteFile` on its pipe would block the broadcast loop. *Mitigation:* each broadcast write uses an overlapped handle with a 500 ms deadline. On timeout, the handle is removed from the registry and closed. The wedged subscriber will reconnect when it recovers; the re-subscribe with `snapshot_request:true` fetches the latest state. Loss is bounded.

**Follow-ups deferred to later phases**
- `BUILTIN\Administrators`-only ACL on ctl-pipe: Phase F. Inline TODO comment in `ctl_server.py`.
- `paths.controller_exe` / `transfer_agent_exe` / `shell_extension_dll`: not added in Phase B's schema. Added in Phase C (supervisor needs `controller_exe`) and Phase D (installer needs `shell_extension_dll`). When added, they go under `paths:`.
- Wiring the currently-unused fields (`limits.max_clipboard_bytes`, `limits.max_file_bytes`, `paths.log_dir`, `proxy.*`, `supervisor.*`): not in Phase B. They stay declared-but-unconsumed until Phase C/D/F.

---

## Post-implementation fix #1 — C# clients fail to subscribe (pipe-name prefix bug)

### Context

Phase B was implemented and all automated tests (9/9 pytest, 7/7 dotnet) passed. During the user's manual smoke test, however, both C# ctl-pipe subscribers (ClipboardInterceptor and Controller) failed with `ctl: subscriber error: The operation has timed out.` repeating on exponential backoff. The Python addon and orchestrator-side ctl_server were fine.

**Root cause.** `NamedPipeClientStream(serverName, pipeName, ...)` expects the **bare** pipe name — i.e., `dlp_agent_ctl`, not the full Windows form `\\.\pipe\dlp_agent_ctl`. .NET internally prepends `\\<server>\pipe\` to whatever name you pass. When the central `config.yaml` stores the canonical Windows form (which it does — `data_pipe: "\\\\.\\pipe\\dlp_agent"`, `ctl_pipe: "\\\\.\\pipe\\dlp_agent_ctl"`), passing that string straight to `NamedPipeClientStream` produces a malformed pipe path like `\\.\pipe\\\.\pipe\dlp_agent_ctl`. `ConnectAsync` waits the full timeout and fails.

**Why pytest didn't catch it.** `scripts/harness/test_ctl_pipe.py` uses a tiny pywin32 ctl-pipe client. pywin32's `win32file.CreateFile(name, ...)` accepts the full Windows path natively. The harness never exercises the C# subscriber, so the bug only surfaces in manual end-to-end runs.

**TransferAgent status — speculation only.** The user did not run TransferAgent in this round and did not provide a log for it. By code inspection: during IT-B7 a private `ExtractPipeName` helper was added to `OrchestratorClient.cs:57-67` that strips the prefix locally. *On that basis* TransferAgent **should** subscribe successfully — but this has NOT been verified end-to-end since the Phase B work was committed. Treat the assertion "TransferAgent works" as inferred-from-code, not observed. If the user later runs TransferAgent via the ShellExtension context menu and it fails the same way, that local helper is the first thing to look at.

### Three affected sites

| # | File:line | Used for | Currently | Status |
|---|---|---|---|---|
| 1 | `src/DlpShared/CtlPipeSubscriber.cs:74-75` | ctl-pipe subscribe in ClipboardInterceptor + Controller | passes raw yaml string to `NamedPipeClientStream(".", _ctlPipeName, ...)` | **BROKEN** — causes the user's reported timeouts |
| 2 | `src/AgentCore/PipeAgentCore.cs:52-53` | data-pipe analyse in ClipboardInterceptor | passes raw yaml string to `NamedPipeClientStream(".", pipeName, ...)` | **BROKEN** — would surface as a clipboard ALLOW/BLOCK timeout the first time the user copies text; user hasn't reached this code path yet |
| 3 | `interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs:52,61-67` | data-pipe analyse in TransferAgent | strips via private `ExtractPipeName` | **code-inspection only** — not run/observed in this round. The private helper exists; on that basis it *should* work, but treat as unverified until the user actually exercises a ShellExtension-triggered transfer. Duplicates logic the other two sites need. |

### Fix

**Add `src/DlpShared/PipeNameHelper.cs`** — a tiny static helper. One public method:

```csharp
public static class PipeNameHelper
{
    private const string FullPrefix = @"\\.\pipe\";

    /// <summary>
    /// Strips the canonical Windows-pipe prefix (\\.\pipe\) if present, returning
    /// the bare pipe name expected by NamedPipeClientStream(serverName, pipeName, ...).
    /// Idempotent: a name that is already bare passes through unchanged.
    /// </summary>
    public static string ToBareName(string name)
    {
        if (string.IsNullOrEmpty(name)) return name;
        return name.StartsWith(FullPrefix, StringComparison.OrdinalIgnoreCase)
            ? name.Substring(FullPrefix.Length)
            : name;
    }
}
```

**Apply at the three call sites:**
- `CtlPipeSubscriber.cs:74-75` — `new NamedPipeClientStream(".", PipeNameHelper.ToBareName(_ctlPipeName), ...)`.
- `PipeAgentCore.cs:52-53` — `new NamedPipeClientStream(".", PipeNameHelper.ToBareName(pipeName), ...)`. Provider may be invoked many times, but `ToBareName` is a cheap one-comparison-and-substring call.
- `OrchestratorClient.cs:52` — replace `ExtractPipeName(dataPipe)` with `PipeNameHelper.ToBareName(dataPipe)`. Delete the private `ExtractPipeName` method (lines 57-67) — the doc-comment on `PipeNameHelper.ToBareName` carries the same explanation.

**Add `src/AgentCore.Tests/PipeNameHelperTests.cs`** — three xUnit `[Fact]`s covering:
- `ToBareName_WithFullPrefix_StripsIt` — input `\\.\pipe\dlp_agent` → output `dlp_agent`.
- `ToBareName_WithBareName_PassesThrough` — input `dlp_agent` → output `dlp_agent` (idempotency).
- `ToBareName_EmptyOrNull_ReturnsAsIs` — input `""` and `null` → returned unchanged.

### Critical files

**Edits**
- `D:\Code\GithubPublishEndpointDLP\src\DlpShared\CtlPipeSubscriber.cs` — 1-line wrap on line 75.
- `D:\Code\GithubPublishEndpointDLP\src\AgentCore\PipeAgentCore.cs` — 1-line wrap on line 52.
- `D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\TransferAgent\OrchestratorClient.cs` — swap call site (line 52) to `PipeNameHelper.ToBareName`, delete private helper (lines 57-67), update XML doc-comment reference.

**New**
- `D:\Code\GithubPublishEndpointDLP\src\DlpShared\PipeNameHelper.cs` — the helper above.
- `D:\Code\GithubPublishEndpointDLP\src\AgentCore.Tests\PipeNameHelperTests.cs` — three cases.

No `.csproj` edits — AgentCore and TransferAgent already reference DlpShared; AgentCore.Tests sees DlpShared transitively via the AgentCore reference.

### Verification

Inside the venv from the repo root:

```powershell
# C# rebuild + unit tests
dotnet build src\DlpShared\DlpShared.csproj
dotnet build src\AgentCore\AgentCore.csproj
dotnet build src\ClipboardInterceptor\ClipboardInterceptor.csproj
dotnet build interceptors\peripheral_storage\Controller\Controller.csproj
dotnet build interceptors\peripheral_storage\TransferAgent\DlpTransferAgent.csproj
dotnet test src\AgentCore.Tests\AgentCore.Tests.csproj   # expect 10 passed: 2 PipeAgentCore + 5 ConfigLocator + 3 PipeNameHelper

# Existing pytest harness must remain green
D:\Code\GithubPublishEndpointDLP\.venv\Scripts\python.exe -m pytest scripts\harness\ -v
```

End-to-end smoke (the user's failing scenario, now expected to succeed):

1. Orchestrator: `python -m orchestrator --foreground --config config.yaml` — log unchanged from the current run.
2. Browser addon: `mitmdump -s interceptors\browser\addon.py --listen-port 8080 --set dlp_config_path=$pwd\config.yaml` — already worked; should still work.
3. **ClipboardInterceptor (the fix target):** `dotnet run --project src/ClipboardInterceptor`. **Expected new behavior:**
   - `[DLP] ctl: subscribed component=clipboard` within ~250 ms (no more timeout-and-backoff loop).
   - Orchestrator log: `ctl: subscribed component=clipboard pid=<N>`.
   - **Additional verification of the second site (PipeAgentCore):** copy any text. Expect `[DLP]` log lines reflecting an ALLOW/BLOCK round-trip over `\\.\pipe\dlp_agent`. Without this fix the data-pipe ConnectAsync would also have timed out — this confirms PipeAgentCore's prefix-strip works.
4. **Controller (the fix target):** `.\UsbDlpController.exe` from its `bin\Debug\net10.0-windows\win-x64\`. **Expected new behavior:**
   - `[Controller] ctl: subscribed component=controller` within ~250 ms.
   - Orchestrator log: `ctl: subscribed component=controller pid=<N>`.
5. **Optional duplicate-launch check (decision #13):** launch a second ClipboardInterceptor; expect `[DLP] ctl: already_subscribed (component=clipboard); backing off`. Stop the first; the second succeeds on its next retry.

### Risks

**RX1 — Idempotency.** `ToBareName` runs on every `AnalyseAsync` invocation via the provider. The single `string.StartsWith` + conditional `string.Substring` is sub-microsecond; no measurable cost.

**RX2 — Case sensitivity of the prefix.** I'm using `StringComparison.OrdinalIgnoreCase` in `ToBareName` because Windows pipe paths are case-insensitive in practice. The legacy private `ExtractPipeName` in TransferAgent used `StringComparison.Ordinal`; the new shared helper is intentionally more permissive (no behavior change for the canonical lowercase form actually emitted by the yaml).

**RX3 — Future similar regressions.** Adding a C#-side end-to-end test that spawns the real C# clients against a real orchestrator subprocess would catch this class of bug. **Not in this fix's scope** — it's a substantial test-harness build-out. Captured here as a follow-up for whoever owns Phase C/D testing infrastructure.

---

## Post-implementation fix #2 — Controller can't find Payload.dll (pre-existing build-layout gap)

### Context

User confirmed Post-impl fix #1 worked: all C# ctl-pipe subscribers now connect. During the same smoke session a second failure surfaced:

```
[Debug] Injecting DLL at: ...\Controller\bin\Debug\net10.0-windows\win-x64\Payload.dll
[Injector] Failed to inject into explorer.exe (PID=6680) - error 9999
```

Error 9999 is a custom Injector code defined in `Controller\Injector.cs:96-100` — it fires when `GetExitCodeThread(hThread, out hModule)` returns `hModule == IntPtr.Zero` after `CreateRemoteThread` ran `LoadLibraryW(path)` in the target. In other words: **the remote `LoadLibraryW` returned NULL** — almost always because the file at `path` doesn't exist on the target's filesystem view (or has unresolved dependencies).

### Root cause (pre-existing; NOT a Phase B regression)

The Controller resolves `payload_dll_path` (default `"Payload.dll"`, relative) against `AppContext.BaseDirectory` (`Controller\Program.cs:53-55`). For a Debug build that resolves to:

```
D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\Controller\bin\Debug\net10.0-windows\win-x64\Payload.dll
```

That file does NOT exist. The actual Payload build output is at:

```
D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\Payload\x64\Debug\Payload.dll    (~20 KB, present)
D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\Payload\x64\Release\Payload.dll  (~18 KB, present)
```

Nothing copies it across. `Controller.csproj` has no `<Content>` rule, no `<Target>` block. `verify-install.ps1` (the legacy installer reference) builds TransferAgent + ShellExtension and never touches Payload. The legacy `Controller/Config/config.yaml` (deleted in Phase B's IT-B8) carried the same `payload_dll_path: Payload.dll` value — so this gap existed identically before Phase B. The Phase B integration plan even tagged peripheral_storage as **"partially integrated"** before this round, and the agent's Phase A verification was orchestrator-only — Controller injection was never end-to-end-tested until now.

Conclusion: **Phase B didn't introduce this bug** — it just brought integration far enough that the user noticed it.

### Fix — MSBuild post-build copy in `Controller.csproj`

Make the dev build layout match the prod layout (Payload.dll sitting next to `UsbDlpController.exe`) by adding a single `<Target>` to `interceptors/peripheral_storage/Controller/Controller.csproj`:

```xml
<Target Name="CopyPayloadDll" AfterTargets="Build">
  <PropertyGroup>
    <PayloadDllSource>$(MSBuildThisFileDirectory)..\Payload\x64\$(Configuration)\Payload.dll</PayloadDllSource>
  </PropertyGroup>
  <Copy SourceFiles="$(PayloadDllSource)"
        DestinationFolder="$(TargetDir)"
        SkipUnchangedFiles="true"
        Condition="Exists('$(PayloadDllSource)')" />
  <Warning Text="Payload.dll not found at $(PayloadDllSource). Controller will fail to inject at runtime. Build the Payload C++ project first: msbuild interceptors\peripheral_storage\Payload\Payload.vcxproj /p:Configuration=$(Configuration) /p:Platform=x64"
           Condition="!Exists('$(PayloadDllSource)')" />
</Target>
```

Why this shape:

- **`AfterTargets="Build"`** runs the copy once the C# build is otherwise complete — no interaction with `dotnet build`'s normal pipeline.
- **`$(MSBuildThisFileDirectory)`** anchors the source path to Controller.csproj's directory (cross-platform-safe, IDE-safe). Resolves to `interceptors\peripheral_storage\Controller\`, so `..\Payload\x64\$(Configuration)\Payload.dll` lands on the right file. Matches the verified-on-disk layout.
- **`$(Configuration)`** substitutes Debug/Release automatically so the copy follows whichever configuration the Controller is being built in.
- **`Condition="Exists(...)"`** keeps the build green when Payload.dll hasn't been built yet (e.g., a fresh checkout, or a developer who is only iterating on C# code). The runtime "error 9999" remains the visible failure in that case — same as today — but now backed by an MSBuild warning at build time so the developer sees the problem before they run.
- **`<Warning>` over `<Error>`**: warn-don't-fail is a deliberate choice. A blocking error would force a Payload build for every Controller build, including times when the developer doesn't care (e.g., editing log strings). The user's existing project-memory note is "Use `dotnet build` for C# .NET projects; MSBuild only for C++ .vcxproj files" — preserving that workflow means keeping the C# build self-sufficient.
- **`SkipUnchangedFiles="true"`** avoids touching `Payload.dll`'s timestamp on rebuilds, which can otherwise cause Windows to flag the DLL as recently modified and disrupt cached injection state in explorer.exe (minor optimization).

No `.csproj` schema change beyond this `<Target>`; no new dependencies; the project still builds the same way (`dotnet build interceptors/peripheral_storage/Controller/Controller.csproj`).

### Critical files

**Edit only**
- `D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\Controller\Controller.csproj` — add the `<Target>` above (insert before `</Project>`).

No new files. No deletions. No changes to `config.yaml`, `AppConfig.cs`, `Program.cs`, `Injector.cs`, `Payload.vcxproj`, or anything in DlpShared/.

### Verification

```powershell
# 1. Build the Payload first (one time, or whenever its source changes).
msbuild interceptors\peripheral_storage\Payload\Payload.vcxproj /p:Configuration=Debug /p:Platform=x64

# 2. Build the Controller. The new MSBuild target should fire and copy Payload.dll.
dotnet build interceptors\peripheral_storage\Controller\Controller.csproj

# 3. Confirm the copy landed:
#    interceptors\peripheral_storage\Controller\bin\Debug\net10.0-windows\win-x64\Payload.dll
#    Should exist and be byte-identical (or at least non-zero, sane size) to:
#    interceptors\peripheral_storage\Payload\x64\Debug\Payload.dll
```

End-to-end smoke (re-run the user's previous scenario, which exposed the bug):

1. Orchestrator: `python -m orchestrator --foreground --config config.yaml` — same as before.
2. Controller (admin Developer PowerShell, in the bin dir): `.\UsbDlpController.exe`. **Expected new behavior:**
   - `[Debug] Injecting DLL at: <Controller bin>\Payload.dll` — same log line.
   - The next line is now **`[Injector] Injected Payload.dll into explorer.exe (PID=...)`** instead of error 9999.
   - From this point the Controller's hot-reload (still subscribed to ctl-pipe per fix #1) keeps working.

Negative-path check: delete `interceptors\peripheral_storage\Payload\x64\Debug\Payload.dll`, then `dotnet build interceptors\peripheral_storage\Controller\Controller.csproj`. Expected:
- Build succeeds with **a warning** ("Payload.dll not found at … Build the Payload C++ project first …"); the Controller exe is still produced.
- Re-running `UsbDlpController.exe` produces error 9999 again — same failure mode as today, no silent regression.

### Risks

**RY1 — Tracking the right Configuration.** If the developer builds Controller with `dotnet build -c Release` and Payload with `msbuild ... /p:Configuration=Debug`, the copy target's `$(Configuration)` resolves to `Release` → source path points at `Payload\x64\Release\Payload.dll`, which won't exist → warning fires → user sees the message and rebuilds Payload as Release. Self-documenting.

**RY2 — IDE incremental builds.** Visual Studio 2026 IDE-driven builds also go through MSBuild; the target fires there too. No special handling needed.

**RY3 — Phase D installer interplay.** When the Phase D installer ships, it'll lay out the prod tree as `%ProgramFiles%\DLP\UsbDlpController.exe` + `%ProgramFiles%\DLP\Payload.dll` (relative resolution still works). The new build target is dev-only; the installer doesn't depend on it (it copies Payload.dll directly from its own source). Captured here so Phase D's planner can confirm the resolution model is shared dev↔prod and no special-casing is needed.

**RY4 — Cross-language build ordering is not enforced.** The plan deliberately does NOT add a `<MSBuild Projects="..\Payload\Payload.vcxproj" />` invocation to the C# target. Reasons:
  1. The user's project memory mandates `dotnet build` for C# and `msbuild` for `.vcxproj`; chaining one from the other erodes that boundary.
  2. Cross-toolchain MSBuild invocations are flaky — they pick up the wrong MSBuild version, hit toolset-mismatch issues, and complicate the developer's mental model.
  3. The warning serves the same purpose without the invasive coupling.

Acknowledging this is a soft contract: a developer who skips the Payload build will see a warning + runtime injection failure. That's good enough for Phase B's scope.

---

## Post-implementation fix #3 — TransferAgent install/verify regressed by Phase B

### Context

User confirmed fixes #1 and #2 work end-to-end. While running the user's normal TransferAgent verification routine (which is **not** `dotnet build`, despite what the Phase B plan said — see below), they hit a regression: the published TransferAgent fails to launch usefully when invoked from the Explorer context menu.

Two distinct issues are bundled here:

**Issue A — `ConfigLocator` walk-up doesn't reach repo root from the `dotnet publish` output.**

`verify-install.ps1` publishes TransferAgent to:

```
D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\TransferAgent\bin\Release\net10.0-windows\win-x64\publish\
```

and registers `HKCU\SOFTWARE\DLPAgent\TransferAgentPath` to the `DlpTransferAgent.exe` inside that dir (script lines 20-22, 72-75). When Explorer's ShellExtension launches that exe, `OrchestratorClient.LoadConfig()` calls `ConfigLocator.FindConfigYaml()` with no `DLP_CONFIG_PATH` env var set. The walk-up anchor is the `publish\` dir. Counting parents:

```
i=0 publish\            (config.yaml? no)
i=1 win-x64\            (no)
i=2 net10.0-windows\    (no)
i=3 Release\            (no)
i=4 bin\                (no)
i=5 TransferAgent\      (no)
i=6 peripheral_storage\ (no)
i=7 interceptors\       (no)
[N=8 loop exits]        — repo root D:\Code\GithubPublishEndpointDLP\ is one hop beyond the window
```

`FindConfigYaml` throws `FileNotFoundException("Could not locate config.yaml. …")`. `Program.Main`'s `try/catch` (added in IT-B7) surfaces this as a MessageBox and exits — to the user, the transfer just "doesn't function as expected." Pre-Phase-B TransferAgent used hardcoded constants and never read a config file, so this was invisible before IT-B7.

The other components don't have this problem because their bin paths are shallower (Controller's bin dir reaches repo root at i=7, ClipboardInterceptor at i=5, the addon at i=2). It's specifically `dotnet publish`'s extra `publish\` layer that pushes TransferAgent over the edge.

**Issue B — IT-B7's `Validates` step in the existing plan referenced a log that doesn't exist.**

The current plan text says:

> "Add a startup log line via `OutputDebugString` or a temp audit file: `loaded orchestrator config from <path>; pipe=<name> timeouts={connect=Xms analysis=Ys}`."

That sentence was speculation — IT-B7 as actually implemented adds **no logging at all** to TransferAgent. TransferAgent is a WinForms app with no console; the only user-visible signal on startup is the MessageBox-on-error path. The user has always validated by running the orchestrator + the install script + restarting explorer + manually triggering a transfer via the right-click UI, and observing **orchestrator-side** logs and the UI's behavior. The plan's "Add a log line" suggestion was never implemented; this fix removes the misleading claim.

Conclusion: Issue A is a real Phase B regression to fix; Issue B is a plan-text correction.

### Fix A — Make `config.yaml` co-located with the published TransferAgent

The smallest correct fix is to teach `DlpTransferAgent.csproj` to **copy the central `config.yaml` into the build/publish output dir**. The published TransferAgent's `ConfigLocator.FindConfigYaml` then finds it at walk-up level 0 (the exe's own dir) — no env var needed, no `verify-install.ps1` change needed, and the same model lines up cleanly with Phase D's installer-driven layout (which will lay `config.yaml` next to the binaries in `%ProgramFiles%\DLP\`).

Add to `interceptors/peripheral_storage/TransferAgent/DlpTransferAgent.csproj`, alongside the existing `<ProjectReference>` ItemGroup:

```xml
<ItemGroup>
  <!-- Post-impl fix #3: include the central config.yaml in build + publish
       output so the published TransferAgent (launched by ShellExtension from
       a deep publish\ dir) finds it via ConfigLocator's walk-up at level 0.
       <Link> places the file at the output root (without it, MSBuild would
       try to preserve the ..\..\..\..\ source-path layout under the output
       dir, which is wrong). PreserveNewest avoids needless re-copy. -->
  <Content Include="..\..\..\config.yaml">
    <Link>config.yaml</Link>
    <CopyToOutputDirectory>PreserveNewest</CopyToOutputDirectory>
    <CopyToPublishDirectory>PreserveNewest</CopyToPublishDirectory>
  </Content>
</ItemGroup>
```

Behavior:
- `dotnet build interceptors/peripheral_storage/TransferAgent/DlpTransferAgent.csproj` → drops `bin\Debug\net10.0-windows\win-x64\config.yaml`.
- `dotnet publish interceptors/peripheral_storage/TransferAgent/DlpTransferAgent.csproj -c Release -r win-x64 --no-self-contained` (which is exactly what `verify-install.ps1` line 44 runs) → drops `bin\Release\net10.0-windows\win-x64\publish\config.yaml`.
- TransferAgent at runtime: `ConfigLocator.FindConfigYaml()` checks the exe's own dir first and finds the copied yaml immediately. Sentinel check (`data_pipe:` top-level key) passes because we're literally copying the canonical file.

`verify-install.ps1` itself needs **no change** — it already invokes `dotnet publish` (line 44), which honors `CopyToPublishDirectory`.

**Staleness model.** Because TransferAgent does a one-shot read at startup (locked decision #9) and is per-file launched (not long-running), a stale `publish\config.yaml` only matters until the next time the operator updates settings. The user's existing workflow ("edit something, re-run `verify-install.ps1`") refreshes both code and config in lockstep. For the Controller and the addon — which DO subscribe to ctl-pipe for live updates — the orchestrator's push-based model continues to apply; this fix changes nothing there.

### Fix B — Correct the IT-B7 verification text + the global Verification section

The misleading "Add a startup log line via OutputDebugString or a temp audit file" sentence in the IT-B7 *Validates* block, and the corresponding line in the *Per-component smoke* table that promises a "Startup log line shows the configured pipe + timeouts", will be edited out and replaced with the user's real workflow:

1. **Build + install:** run `interceptors\peripheral_storage\verify-install.ps1` from a VS 2026 Developer PowerShell. That script does `dotnet publish` (which now also copies `config.yaml`) + builds ShellExtension + writes the four HKCU registry keys. **(NOT `dotnet build` — the build alone doesn't update the path that HKCU points to, so the context menu would still invoke a stale exe.)**
2. **Restart Explorer:** `taskkill /f /im explorer.exe; explorer.exe` (Task Manager → "Restart" achieves the same). This is needed so the new ShellExtension DLL is loaded into the fresh explorer.exe.
3. **Start orchestrator** (separate PowerShell, venv): `python -m orchestrator --foreground --config config.yaml`.
4. **Plug in a removable drive, right-click a file → "Transfer to USB (DLP Protected)".**
5. **What to look at — there is NO TransferAgent-side log file.** Confirmation lives in two places:
   - **TransferForm UI behavior** — the form opens, lists the files, shows per-file ALLOW/BLOCK as the analysis returns, and copies the allowed ones. If `LoadConfig()` failed, you instead get the MessageBox `"DLP Transfer Agent could not load its configuration."` with a stack-of-paths-tried — the immediate signal that the central yaml isn't reachable from the publish dir.
   - **Orchestrator console** — a healthy transfer produces a `recv req=… channel=peripheral_storage kind=file size=…` debug line and a corresponding `ALLOW`/`BLOCK` line (see `orchestrator/server.py:131-132` for the format). No analysis line means the TransferAgent never reached the orchestrator — likely a config-loading failure or a pipe-name mismatch.

### Critical files

**Edit only**
- `D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\TransferAgent\DlpTransferAgent.csproj` — add the `<Content>` ItemGroup above.
- `D:\Code\GithubPublishEndpointDLP\C:\Users\PocketBaguette\.claude\plans\code-base-brief-this-mighty-boot.md` (this file) — minor corrections to IT-B7 `Validates`, the global Verification section's "TransferAgent" smoke bullet, and the build-commands list (point at `verify-install.ps1` for TransferAgent rather than `dotnet build`).

**Not changed**
- `verify-install.ps1` — unchanged. `dotnet publish` already honors the new `<Content>` include.
- `OrchestratorClient.cs` / `Program.cs` / `ConfigLocator.cs` — unchanged. The fix is build-system-only; the runtime code path is identical.
- `ConfigLocator.FindConfigYaml`'s N=8 — deliberately NOT bumped. The walk-up is meant to handle the `bin\Debug\…` dev case for processes that run from their build dirs (Controller, ClipboardInterceptor), not the additional `publish\` layer that `dotnet publish` adds. Bumping N would mask the real fix (co-locating config.yaml with the exe) and introduce a higher false-positive surface for unrelated `config.yaml` files higher up in the filesystem. The Content+Link approach is the cleaner, more local solution.

### Verification

1. **Rebuild + reinstall via the user's normal workflow:**

   ```powershell
   # From VS 2026 Developer PowerShell (msbuild on PATH):
   cd D:\Code\GithubPublishEndpointDLP
   .\interceptors\peripheral_storage\verify-install.ps1
   ```

   Expect `[1/5] Build C# TransferAgent` to succeed, output `DlpTransferAgent.exe` at the publish path, and after the script completes, the publish dir should contain a copy of `config.yaml`:

   ```powershell
   Test-Path .\interceptors\peripheral_storage\TransferAgent\bin\Release\net10.0-windows\win-x64\publish\config.yaml
   # Expected: True
   ```

2. **Restart explorer + trigger:** Task Manager → "Restart Windows Explorer" → orchestrator running in a separate console → right-click a small text file on the desktop → "Transfer to USB (DLP Protected)". The TransferForm should appear (no MessageBox); the orchestrator console should log a `recv req=… channel=peripheral_storage kind=file` line; the TransferForm should report ALLOW/BLOCK per the analyzer's decision.

3. **Negative-path sanity** (optional): manually delete the published `config.yaml` (`del .\interceptors\peripheral_storage\TransferAgent\bin\Release\net10.0-windows\win-x64\publish\config.yaml`), restart explorer, and trigger a transfer. You should now see the MessageBox `"DLP Transfer Agent could not load its configuration."` — confirming that the fix is what made it work in step 2 (and not something incidental).

4. **No regression in other suites:**

   ```powershell
   D:\Code\GithubPublishEndpointDLP\.venv\Scripts\python.exe -m pytest scripts\harness\ -v
   dotnet test src\AgentCore.Tests\AgentCore.Tests.csproj
   ```

   Both should still pass (9 + 10).

### Risks

**RZ1 — Stale `publish\config.yaml` if the operator edits the repo's `config.yaml` without re-running `verify-install.ps1`.** Acceptable, because TransferAgent reads at startup only (decision #9), and the user's existing workflow always re-runs the script after non-trivial changes. The hot-reload model (orchestrator pushes via ctl-pipe to Controller, ClipboardInterceptor, browser addon) is unaffected — TransferAgent intentionally doesn't subscribe.

**RZ2 — `<Content Include="..\..\..\config.yaml">` reaches outside the project tree.** MSBuild handles this fine — `<Link>config.yaml</Link>` tells it to place the file at the output root, not preserve the `..\..\..\` source-path layout. This is a documented and standard MSBuild pattern. (The relative path is three `..\` because the csproj sits at `interceptors\peripheral_storage\TransferAgent\` — same depth as the existing `<ProjectReference Include="..\..\..\src\DlpShared\…" />` in the same file.)

**RZ3 — The Phase D installer's layout already plans for `%ProgramFiles%\DLP\config.yaml` next to the binaries.** This fix uses the same model. Phase D will replace `verify-install.ps1` and copy `config.yaml` directly; the `<Content>` include is dev-only flavoring of the same resolution model. No future-Phase friction.

**RZ4 — TransferAgent debuggability is still thin.** With this fix, success is silent (orchestrator-side logging is the proxy) and failure is a MessageBox. If future investigation of a TransferAgent issue becomes hard, adding a small `%TEMP%\dlp-transfer-agent-<pid>.log` (one line per LoadConfig + one per AnalyzeAsync result) would be a cheap improvement. **Out of scope for this fix** — it goes beyond what the user asked for and beyond what was claimed by Phase B's plan.
