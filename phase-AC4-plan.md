# Phase AC-4 — `dlp-ctl` App Control authoring workflow + admin-pipe commands: Detailed Plan

## Status — ✅ COMPLETE (VM-confirmed 2026-06-13)

All tasks AC4-T1..T9 done. **Dev gates green:** AC-4 unit tests 13 passed (V1); full
harness **106 passed / 3 skipped** (V2, was 93/3); offline list mgmt (V3); a **real
`ConvertFrom-CIPolicy` build** of `notepad.exe` → valid 3104-byte `.cip` self-validated
clean and `apply`'d atomically to a temp inbox (V4/V5); `status` degrades gracefully when
the agent is down (V6). **VM loop PASSED** on the installed `DLPAgent` service (build
26200): on-VM `dlp-ctl appcontrol build`/`apply` auto-deployed via the AC-3 watcher
(deploy line in events.jsonl, `{GUID}` `IsEnforced:true`), a denied app blocked + an
allowed app ran with block counters surfacing in `dlp-ctl appcontrol status`, `disable`
removed the policy over the admin-pipe, and `disable --force-local` removed it without a
healthy agent — all no-reboot, clean return to baseline. The ConfigCI DISM enable was the
expected one-time VM prerequisite (P4); AC-5's `enable_configci` step will automate it.

Shipped: `orchestrator/app_control/{paths,builder}.py`; edits to
`orchestrator/{ctl,admin_server,__main__,channel}.py` + `README.md`; tests
`scripts/harness/test_app_control_ctl.py` (13 cases). **AC-5 is unblocked.**

## Context

The App Control (WDAC) channel is being integrated into the DLP agent (`app-control-integration-plan.md`). **AC-1** (VM spike), **AC-2** (pure-logic WDAC engine + manifest validators), and **AC-3** (in-orchestrator inbox watcher + `citool` deployer + CodeIntegrity event forwarder, VM-confirmed) are complete. Today a policy reaches the running agent only by hand-building a push with `scripts/build-ac3-inbox.py` and copying the subfolder into `%ProgramData%\DLP\appcontrol\inbox\`. There is **no operator workflow** to author, compile, stage, deploy, inspect, or emergency-disable a policy.

**AC-4 delivers the standalone operator loop** as `dlp-ctl appcontrol allow|deny|build|apply|status|disable`. This is the "standalone mode" of the parent vision (no central server yet): the admin maintains allow/deny lists, builds + compiles a `.cip` on the endpoint, applies it (the explicit go-live gate), watches block/deploy events, and can disable in an emergency. `build`/`apply`/`allow`/`deny` are **offline** local-file operations (operator is elevated); `status`/`disable` talk to the running agent over the Administrators-only admin-pipe, with a `--force-local` escape hatch for `disable` when the agent is dead.

**Intended outcome:** an admin on a clean endpoint can drive the entire policy lifecycle with `dlp-ctl appcontrol …`, and the existing AC-3 inbox watcher deploys what `apply` drops — no hand-built pushes, no central server required.

## Locked decisions (this planning session)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Generalized `commands` dict on `AdminServer`.** Add an optional `commands: dict[str, Callable[[dict], dict]] \| None = None`; `handle_request` routes any cmd found there. `status`/`reload` stay as the existing positional callbacks (existing `test_handle_request_dispatch` unaffected). | Resolves parent open question #3. `appcontrol_disable` (and future server/appcontrol commands) become one-line dict entries — extensible, minimal, backward-compatible. |
| D2 | **`appcontrol status` reuses the existing `{"cmd":"status"}` admin command.** ctl pulls `resp["app_control"]` (which `channel.status()` already populates, incl. an on-demand `reconcile()`) and renders it in detail, plus reads local staging/list state client-side. **No new `appcontrol_status` admin command.** | `channel.status()` already returns policy_guid/version/blocks/pending/rejected/last_error and self-heals via reconcile. A dedicated command would be a near-duplicate code path. Smaller admin surface. |
| D3 | **Authoring surface = InternalName FileAttrib default + automatic Hash-fallback.** `allow`/`deny` store raw file/folder paths; folders are scanned recursively for executables at **build** time; each resolved file gets a FileAttrib rule on its **InternalName** (AC-2 default), and a file with no usable PE version-info automatically falls back to a WDAC **Hash** rule (parent decision 5). No `--level`/`--filepath`/`--hash` flags in AC-4. | Simplest operator UX; matches AC-1/AC-2 proven behavior. `policy_xml`/`hashing` still expose the richer levels for a later phase. |
| D4 | **`build` → `staging\build\`; `apply` = atomic directory rename → `inbox\<utc>_build\`.** A push is a subfolder `{policy.xml, {GUID}.cip, manifest.json}`. Staging and inbox are both under `%ProgramData%\DLP\appcontrol\` (same volume), so `os.replace(staging\build, inbox\<utc>_build)` renames the whole subfolder atomically — the entire push appears at once, trivially satisfying the watcher's "manifest last" + size-stable pickup (≤1 extra poll). | No partial-read race; reuses the AC-3 watcher unchanged. |
| D5 | **`build` self-validates with `manifest.validate_all` and raises on any failure** — a malformed/under-versioned/self-protect-missing build never reaches `staging`, so `apply` can only ship a push the AC-3 watcher will accept. | Fail-closed; mirrors `build-ac3-inbox.py`'s dev self-check. |

Inherited: parent decisions 1 (on-endpoint compile via ConfigCI), 2 (pre-compiled `.cip` shipped to the watcher), 3 (self-protect validate-and-reject), 4 (remove + neutralizer disable path — AC-3's `deployer.remove()`), 5 (hash fallback), 7 (idle until first push). The AC-2 carry-forward (every built policy includes `<install_root>\*` **and** `C:\Program Files\dotnet\*` self-protect FilePath rules) is satisfied automatically by `selfprotect.add_selfprotect_rules`.

## Architecture

```
dlp-ctl (orchestrator/ctl.py)                         elevated operator CLI
  appcontrol allow|deny  ──► builder.add/remove_entries   → {allow,deny}-list.txt   (OFFLINE)
  appcontrol build       ──► builder.build()              → staging\build\{xml,cip,manifest}  (OFFLINE)
  appcontrol apply       ──► builder.apply()              → inbox\<utc>_build\ (atomic rename)  (OFFLINE)
  appcontrol status      ──► admin-pipe {"cmd":"status"}  → render resp["app_control"] + local staging/lists
  appcontrol disable     ──► admin-pipe {"cmd":"appcontrol_disable"} → channel.disable() → deployer.remove()
  appcontrol disable --force-local ──► builder.disable_local()  → Deployer.remove() in-process  (OFFLINE escape)

orchestrator/app_control/
  paths.py    (NEW)  pure dir/file resolution shared by channel.py + builder.py
  builder.py  (NEW)  list mgmt · build (engine+compile+manifest) · apply · disable_local
  channel.py  (EDIT) + AppControlChannel.disable(); resolution via paths.py
  + reuses: policy_xml, selfprotect, manifest, hashing, deployer (all AC-2/AC-3, unchanged in behavior)

orchestrator/admin_server.py (EDIT)  optional commands dict routing
orchestrator/__main__.py     (EDIT)  wire commands={"appcontrol_disable": _appcontrol_disable}
```

- **`builder.py` is import-light** (no analyzer deps): it pulls only `policy_xml`/`selfprotect`/`manifest`/`hashing`/`deployer`/`paths` (which lazily import `win32api`/`subprocess`). `ctl.py` **lazy-imports `builder` only inside the `appcontrol` branch**, so `status`/`reload`/`tail` stay as light as today (PF#6 constraint).
- **Reuse, not reinvention:** `build()` is the productionized `scripts/build-ac3-inbox.py` flow (`load_base_policy` → `set_version_ex`/`set_policy_info_id` → `add_file_attrib_rule`/`add_hash_rules` → `add_selfprotect_rules` → `serialize` → ConvertFrom-CIPolicy → `flat_sha256` manifest). List scanning ports `collect_files`/`read_list_file` from `interceptors/app_control/cli/add-wdacwrule.py`. Compile preflight reuses `hashing._preflight_configci`'s DISM-hint pattern. `disable_local` constructs the AC-3 `Deployer` and calls its existing `remove()`.

## Implementation tasks (isolated, sequential)

### AC4-T1 — `paths.py`: shared App Control path resolution (refactor)
- **New** `orchestrator/app_control/paths.py` — pure functions taking `config`: `appcontrol_root()`, `inbox_dir()`, `rejected_dir()`, `staging_dir()`, `status_path()`, `install_root()`, `dotnet_root()`, `extra_paths()`, `allow_list_path()`, `deny_list_path()`. Logic lifted **verbatim** from `channel.py.__init__` (config override → `%PROGRAMDATA%\DLP\appcontrol\<sub>` / `config.raw["install"]["state_dir"]` / `%ProgramFiles%\DLP` / `selfprotect.default_dotnet_root()`). Lists default to `appcontrol_root()/("allow-list.txt"|"deny-list.txt")`.
- **Edit** `channel.py` to call these (behavior-preserving — same values).
- **Acceptance:** `channel.py` resolves byte-identical dirs to today; builder and channel agree on `inbox_dir`/`staging_dir` (so `apply` writes where the watcher reads).

### AC4-T2 — `builder.py`: allow/deny list management
- Port `read_list_file` (one path/line, `#`-comments, BOM-tolerant) and `collect_files` (recursive `rglob`, **executable-extension filter** `.exe/.dll/.sys/.ocx/.scr/.cpl/.efi`, absolute paths, missing-path error) from `add-wdacwrule.py`.
- `read_entries(list_path)`, `add_entries(list_path, paths)`, `remove_entries(list_path, paths)` — store **raw** file/folder paths (folders re-scanned each build), set-dedup (case-insensitive on Windows), stable order, atomic write (temp+`os.replace`, the repo convention).
- **Acceptance:** unit test — add dedups + persists; `--remove` deletes; folders stored raw (expansion happens at build).

### AC4-T3 — `builder.py`: `build()` (engine → compile → manifest → staging)
- `build(config, *, version=None, compiler=_default_compiler, powershell="powershell") -> BuildResult`:
  1. Resolve via `paths.py`; `collect_files` the allow + deny lists.
  2. `doc = px.load_base_policy()`. For each file: `attr = px.read_file_attribute(f, "InternalName")`; if `attr`: `px.add_file_attrib_rule(doc, "InternalName", attr, allow=…)` + `px.warn_on_risky_attribute(...)`; else (no PE info) `add_hash_rules(doc, f.name, hashing.compute_wdac_hashes(f, powershell=…), allow=…)`. Track `{rules, hashed, skipped}` for the report.
  3. `sp.add_selfprotect_rules(doc, install_root, dotnet_root=…, extra_paths=…)` (the AC-2 carry-forward — always).
  4. Version: `next = bump(max(deployed_version_ex, base_floor=get_version_ex(base), staged_version_if_any))`; `set_version_ex` + `set_policy_info_id(version)`. `--version` overrides (validated 4-part, must exceed deployed).
  5. Clear `staging\build\`; `serialize` → `policy.xml`; `compiler(xml, cip)` → `{GUID}.cip` (default: **Windows PowerShell** `ConvertFrom-CIPolicy`, preflight ConfigCI → raise the DISM hint if absent — reuse `hashing._preflight_configci`).
  6. Write `manifest.json` (schema 1, `flat_sha256` of xml+cip — reuse `mf.flat_sha256`).
  7. **Self-validate:** `mf.validate_all(parse_manifest(...), staging\build, deployed_version_ex=…, install_root=…, …)` → raise `BuildError` listing failures if non-empty (D5).
- **Compiler is the injectable seam** (default real ConvertFrom-CIPolicy; tests pass a stub that writes a dummy `.cip`). `powershell` defaults to `powershell` (Windows PowerShell 5.1 — ConfigCI's host; **not** `pwsh`, which has known FilePath-rule issues).
- **Acceptance:** unit test (stub compiler + monkeypatched `read_file_attribute`/`compute_wdac_hashes`) — staging gets xml+cip+manifest; version bumps above a faked deployed; `validate_all` clean; a no-metadata file routes through the hash-fallback.

### AC4-T4 — `builder.py`: `apply()` + `disable_local()`
- `apply(config) -> dict`: require `staging\build\manifest.json`; `inbox_dir().mkdir(parents=True, exist_ok=True)`; `os.replace(staging\build, inbox\<utc>_build)` (atomic rename, D4). Return the inbox subfolder. Error clearly if no staged build.
- `disable_local(config) -> dict`: build `Deployer(status_path=paths.status_path(config), policy_id=px.get_policy_id(px.load_base_policy()))` and call `remove()`; return `{"removed": bool}`. The `--force-local` escape hatch — drives `citool` in-process, no dependency on a healthy agent.
- **Acceptance:** unit test — apply moves staging→inbox (staging emptied, inbox subfolder has all three files); `disable_local` calls `Deployer.remove()` with an injected citool runner (no real shell-out).

### AC4-T5 — `admin_server.py`: generalized `commands` dict + `appcontrol_disable`
- `__init__(..., commands: dict[str, Callable[[dict], dict]] | None = None)`; store `self._commands = commands or {}`.
- In `handle_request`, after the `status`/`reload` branches and before the unknown-cmd fallthrough: `if cmd in self._commands: return {"ok": True, **self._commands[cmd](request)}` (inside the existing try/except that already shields the loop).
- **Acceptance:** unit test — `handle_request({"cmd":"appcontrol_disable"})` calls the injected handler; unknown cmd still errors; `status`/`reload` unchanged (existing `test_handle_request_dispatch` still passes with no `commands` arg).

### AC4-T6 — `__main__.py` + `channel.py`: wire the disable callback
- `channel.py`: add `AppControlChannel.disable() -> dict` → guard `self._deployer`; `removed = self._deployer.remove()`; return `{"removed": removed, **self.status()}`.
- `__main__.py`: define `_appcontrol_disable(request) -> dict` (reads the later-assigned `app_control_channel` at call time, exactly like `_status_provider`; returns `{"removed": False, "error": "app control channel not running"}` when `None`). Pass `AdminServer(config, _status_provider, _reload_callback, commands={"appcontrol_disable": _appcontrol_disable})`.
- **Acceptance:** harness `test_admin` still green; a unit test of `_appcontrol_disable` with a fake channel returns `{"removed": True, ...}`.

### AC4-T7 — `ctl.py`: `appcontrol` subparser
- Add an `appcontrol` subparser with nested subcommands (a second-level `add_subparsers`): `allow`/`deny` (`paths` nargs='+', `--remove`), `build` (`--version`), `apply`, `status`, `disable` (`--force-local`). All accept the shared `--config`.
- **Lazy-import** `from orchestrator.app_control import builder` inside the `appcontrol` dispatch only.
- Routing: `allow/deny/build/apply` → `builder.*` (offline; print a concise report — files added, rules/hashed/skipped counts + risky-name warnings, staged version, "run `dlp-ctl appcontrol apply` to go live"). `status` → `_admin_call({"cmd":"status"})`, render `resp["app_control"]` in detail + a local `staging`/`{allow,deny}-list` summary (offline fallback: if the pipe is down, still print the local + `appcontrol_status.json` view). `disable` → `_admin_call({"cmd":"appcontrol_disable"})`; `--force-local` → `builder.disable_local(config)`.
- Reuse existing `_admin_call` (friendly not-running / access-denied messaging).
- **Acceptance:** `python -m orchestrator.ctl appcontrol --help` and each subcommand parse; offline `allow`/`build` mutate files; `status` degrades gracefully when the agent is down.

### AC4-T8 — tests `scripts/harness/test_app_control_ctl.py`
Pure/in-process (style mirrors `test_app_control_inbox.py` + `test_admin.py`; per-test tmp dirs via the conftest `PROGRAMDATA` pattern or explicit paths in a tmp `config`):
- list management (add/remove/dedup/folder-stored-raw);
- `build` with stub compiler + monkeypatched `read_file_attribute` (FileAttrib path) and one no-metadata file → monkeypatched `compute_wdac_hashes` (hash-fallback path); assert staging contents, manifest `validate_all` clean, version > deployed;
- `apply` (staging→inbox subfolder; staging emptied) and re-`apply` with empty staging errors;
- `disable_local` with injected citool runner → `remove()` path;
- `admin_server` commands-dict routing (T5) + `_appcontrol_disable` wrapper (T6);
- `ctl` arg-parse smoke for every `appcontrol` subcommand.
- **Acceptance:** `pytest scripts/harness/test_app_control_ctl.py -v` green; full `pytest scripts/harness` stays green.

### AC4-T9 — README + `config.yaml` doc touch (light)
- README §B "Admin CLI (`dlp-ctl`)" + the manual-test section: add the `appcontrol` subcommands and the standalone loop (allow → build → apply → status → disable). Note `build` needs ConfigCI on the endpoint (AC-5 will auto-enable; until then the AC-1 DISM step is a prerequisite).
- No `config.py`/`config.yaml` schema change (AC-3 already added the `app_control:` section; lists/staging resolve from it).

## Verification

**Discipline:** every **dev-side** step below I run and confirm green **before** handoff (the AC-2/AC-3 practice). The real `ConvertFrom-CIPolicy` compile is exercised on dev (ConfigCI present per AC-1/AC-2) writing only to `tmp\` — fully undoable, never deploys. **VM-side** steps are yours to run; each has a pass check + rollback, and reuses AC-1/AC-3-validated `citool` mechanics verbatim.

### Dev-side (pre-tested before handoff; repo root, `.venv`, Python 3.13)

| Step | Command | Pass check |
|---|---|---|
| **V1** AC-4 unit tests | `python -m pytest scripts/harness/test_app_control_ctl.py -v` | all `PASSED`, 0 failed |
| **V2** full harness regression | `python -m pytest scripts/harness` | **0 failed**; only the 3 pre-existing admin-pipe elevation tests `skipped` (baseline 93 passed / 3 skipped + new) |
| **V3** offline list mgmt | `python -m orchestrator.ctl appcontrol allow C:\Windows\System32\notepad.exe --config <tmp config>` then `... appcontrol deny <dir> --config …` | `{allow,deny}-list.txt` created/updated under the tmp appcontrol root; folder stored raw |
| **V4** real build → staging (side-effect-free) | `python -m orchestrator.ctl appcontrol build --config <tmp config>` (allow list = `notepad.exe`; dirs point at `tmp\ac4\`) | `staging\build\{policy.xml, {GUID}.cip, manifest.json}` produced; real `ConvertFrom-CIPolicy` exits 0; printed report shows 1 rule + self-protect; **`mf.validate_all` clean** |
| **V5** apply (offline) | `python -m orchestrator.ctl appcontrol apply --config <tmp config>` | `staging\build` gone; `inbox\<utc>_build\` has all three files (the AC-3 watcher would pick it up) |
| **V6** status degrades when agent down | `python -m orchestrator.ctl appcontrol status --config <tmp config>` (no agent) | prints local staging/list + `appcontrol_status.json` view, not a traceback; "agent not running" note for the live fields |
| **V7** hash-fallback real path (spot) | build with an allow entry that has no PE version-info (a copied `pcre2-8.dll`-style file) | routes to `New-CIPolicy -Level Hash` (4 hashes) — or covered by the T8 unit test if no such file is handy on dev; **marked** below |

> **Marked / can't-fully-pre-test on dev:** V7's *real* `New-CIPolicy -Level Hash` shell-out was already VM/dev-verified in **AC-2 V5**; here it's covered by a stubbed unit test plus, opportunistically, a real run if a no-version-info PE is available on the dev box. Reason: depends on having a metadata-less PE handy; the code path itself is unchanged from AC-2's verified `hashing.compute_wdac_hashes`.

### VM-side (installed `DLPAgent` service, build 26200 — user-executed)

`{GUID}` = `{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}`. `%AC%` = `C:\ProgramData\DLP\appcontrol`. Every `citool` call wrapped `cmd /c "echo . | citool … --json"` (AC-1 stdin finding). Run `dlp-ctl appcontrol …` from a **NEW elevated** shell (PATH wrapper).

**Prerequisites (each has a check):**
- **P1** .NET 10 Desktop Runtime present (`dotnet --list-runtimes` → `Microsoft.WindowsDesktop.App 10.x`).
- **P2** VMware snapshot `pre-AC4` taken now.
- **P3** AC-4 bundle installed (`install.cmd` as admin); `Get-Service DLPAgent` → Running; `%AC%\{inbox,rejected,staging}` exist; **no** policy deployed (`dlp-ctl status` → `app-control: … no policy`).
- **P4 — ConfigCI enabled on the VM** so on-VM `dlp-ctl appcontrol build` can compile (the AC-1 DISM loop, one-time): `Get-ChildItem $Env:SystemRoot\servicing\Packages\*ConfigCI*.mum | % { dism /online /norestart /add-package:"$($_.FullName)" }` → then `Get-Command ConvertFrom-CIPolicy` resolves. **Marked:** this manual step is what **AC-5's `enable_configci` installer step will automate**; for AC-4 it is a stated prerequisite (leaving ConfigCI enabled is benign; the `pre-AC4` snapshot covers rollback). *Alternative if you prefer not to enable ConfigCI on the VM:* build the `.cip` on dev (V4) and copy `staging\build\` into `%AC%\inbox\` to exercise apply/deploy/status/disable only — but that skips the on-VM `build`, which is the AC-4 deliverable.

| # | Action | Pass check | Rollback |
|---|---|---|---|
| **S0** baseline | `dlp-ctl appcontrol status` | enabled, running, no policy, pending=0 | — |
| **S1** author lists | `dlp-ctl appcontrol deny "C:\Program Files\Microsoft OneDrive\OneDrive.exe"` ; `dlp-ctl appcontrol allow "C:\Program Files\7-Zip"` (or any installed app folder) | `deny-list.txt`/`allow-list.txt` updated; risky-name warnings shown where applicable | delete the list files |
| **S2** build | `dlp-ctl appcontrol build` | `staging\build\{policy.xml,{GUID}.cip,manifest.json}`; report shows rules + self-protect + version > deployed; ConvertFrom-CIPolicy exits 0 | clear `staging\build` |
| **S3** apply | `dlp-ctl appcontrol apply` | `staging\build` consumed; `%AC%\inbox\<utc>_build\` appears | S7 |
| **S4** auto-deploy | within ~poll_seconds: `dlp-ctl appcontrol status` ; `cmd /c "echo . \| citool --list-policies --json"` ; tail `events.jsonl` | status shows deployed `{GUID}` + version; list shows `{GUID}` `IsEnforced:true`; `events.jsonl` has `{"event":"deploy","outcome":"ok"}` | S7 |
| **S5** enforce | launch denied `OneDrive.exe` (blocked) and an **allowed** app (runs) | deny blocked; allowed app runs; `dlp-ctl appcontrol status` block counters increment; `"event":"block"` lines | S7 |
| **S6** disable (live) | `dlp-ctl appcontrol disable` | returns removed=true; `dlp-ctl appcontrol status` → no policy; `{GUID}` gone from `--list-policies`; OneDrive runs again | S7 |
| **S7** force-local + teardown | (if agent stopped) `dlp-ctl appcontrol disable --force-local` ; else `cmd /c "echo . \| citool --remove-policy ""{GUID}"" --json"` ; `Restart-Service DLPAgent` | `{GUID}` gone, **no reboot**; baseline restored; all apps run | snapshot `pre-AC4` |

**Passes when:** S2 builds + compiles on the VM, S3→S4 auto-deploys via the AC-3 watcher, S5 enforces, S6 disables cleanly over the pipe, and S7's `--force-local` removes the policy without a healthy agent. **Rollback ladder (AC-1/AC-3-proven, no reboot):** `dlp-ctl appcontrol disable --force-local` → `citool --remove-policy "{GUID}"` → shipped `neutralizer.cip` + refresh → snapshot `pre-AC4`.

## Files

**New:** `orchestrator/app_control/paths.py`, `orchestrator/app_control/builder.py`, `scripts/harness/test_app_control_ctl.py`.
**Edit:** `orchestrator/app_control/channel.py` (paths.py + `disable()`), `orchestrator/admin_server.py` (commands dict), `orchestrator/__main__.py` (wire `appcontrol_disable`), `orchestrator/ctl.py` (`appcontrol` subparser), `README.md` (CLI docs).
**Unchanged (reused):** `policy_xml.py`, `selfprotect.py`, `manifest.py`, `hashing.py`, `deployer.py`, `neutralizer.py`, `events.py`, `config.py`, `config.yaml`.

## Out of scope / downstream
- **AC-5** — installer `appcontrol_dirs` / `enable_configci` (automates P4) / `appcontrol_policy_guard` steps; `package-bundle.ps1` confirmation; clean-VM acceptance. AC-4's `builder` + `paths` are what the installer's dir/guard steps will reuse.
- No central server (the inbox contract AC-4 writes to is the same one a future server transport will use).

## Web-verified (this session)
- `ConvertFrom-CIPolicy` and `New-CIPolicy` are **current** ConfigCI cmdlets (Windows Server 2025 PS docs, updated March 2026 — not deprecated/removed): [ConvertFrom-CIPolicy](https://learn.microsoft.com/en-us/powershell/module/configci/convertfrom-cipolicy?view=windowsserver2025-ps), [New-CIPolicy](https://learn.microsoft.com/en-us/powershell/module/configci/new-cipolicy?view=windowsserver2025-ps). Use **Windows PowerShell 5.1** as the ConfigCI host (`powershell`, not `pwsh` — known FilePath-rule issues in PS7). `citool --remove-policy` no-reboot + the neutralizer fallback are AC-1/AC-3 VM-proven.

## Parent-plan edits to apply on completion (mirroring AC-2/AC-3 close-out)
Mark `### Phase AC-4` ✅ COMPLETED with an Outcome paragraph; strike cross-cutting open question #3 (resolved: generalized `commands` dict); note `appcontrol status` reuses `{"cmd":"status"}` and the InternalName-default + auto-hash-fallback authoring scope.
