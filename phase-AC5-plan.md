# Phase AC-5 — Installer / bundle integration + clean-VM end-to-end: Detailed Plan

## Status — ✅ COMPLETE (VM-confirmed 2026-06-15)

All tasks AC5-T1..T6 + the uninstall-under-enforcement Follow-up (F1..F4) done. **Dev gates green:**
installer unit tests 21 passed; full harness **119 passed / 3 skipped** (was 106/3 at AC-4 + the
AC-5 cases); read-only DISM glob, bundle-config `app_control` check, and `package-bundle.ps1`
parse-check all green; rendered installed `uninstall.cmd` inspected OK.

**Clean-VM acceptance PASSED** on Win11 Home 26200 after two VM-found fixes:
1. **S2 rc=14107** — the servicing dir holds several ConfigCI `.mum` revisions and only the one
   matching the current CU applies; the rest fail "not applicable". Fixed: `enable_configci` now
   continues past per-package failures and decides success via a **post-DISM probe**
   (`ConvertFrom-CIPolicy` must resolve), idempotent + still fail-closed.
2. **S8 uninstall-under-policy** — the deployed self-protect policy blocks the bundle's embed python
   (PSF-signed, off-tree), so the bundle `uninstall.cmd` couldn't launch under enforcement. Fixed:
   uninstall runs from the **installed** python (WDAC-allowed by `<install_root>\*`); the bundle
   `uninstall.cmd` prefers it, and the installer drops a self-relaunching `<install_root>\uninstall.cmd`
   for the bundle-gone case.

With both fixes the full S0–S9 loop passed: ConfigCI auto-enabled on a clean box (S2), on-endpoint
build→apply→enforce (S4–S5), decision-8 8a/8b (S6), live disable (S7), uninstall-under-enforcement
strips everything via both uninstallers (S8a/S8b), reinstall clean (S9). **AC-5 done → the whole App
Control channel integration AC-1…AC-5 is complete.**

## Context

The App Control (WDAC — Windows Defender Application Control, a.k.a. "App Control for
Business") channel is being folded into the DLP agent (parent: `app-control-integration-plan.md`).
**AC-1** (VM spike), **AC-2** (pure-logic WDAC engine + manifest validators), **AC-3**
(in-orchestrator inbox watcher + `citool` deployer + CodeIntegrity event forwarder, VM-confirmed),
and **AC-4** (`dlp-ctl appcontrol allow|deny|build|apply|status|disable`, VM-confirmed) are
complete.

**The gap AC-5 closes:** the channel's code ships, but the **installer** (`orchestrator/installer.py`,
the transactional `--install` / `--uninstall` driver) knows nothing about App Control. Three
things are missing on a freshly-installed endpoint:

1. The drop-folder tree `%ProgramData%\DLP\appcontrol\{inbox,rejected,staging}` is created only
   lazily by the running service (`channel.start()`), and **nothing removes it (or the operator's
   allow/deny lists and any pushes) at uninstall** — violating parent decision 7 ("everything
   stripped at uninstall").
2. **ConfigCI** — the PowerShell module that provides `ConvertFrom-CIPolicy` (compiles a policy
   XML → binary `.cip`) and `New-CIPolicy` (the hash-rule fallback) — is **absent by default on
   Win11 Home**. Without it, on-endpoint `dlp-ctl appcontrol build` fails. AC-4 required a *manual*
   one-time DISM enable as a VM prerequisite (its "P4"); AC-5 automates it.
   - *Jargon:* **DISM** = `Dism.exe`, the built-in Windows servicing tool. **`.mum`** = a Microsoft
     Update Manifest describing an OS component package; the ConfigCI feature ships as ~12 `.mum`
     packages staged under `%SystemRoot%\servicing\Packages\` but not installed on Home. The
     AC-1-proven recipe installs them offline with `dism /online /norestart /add-package:<path>`.
3. **Uninstall does not remove a deployed enforcement policy.** Install deploys *no* policy
   (decision 7 — the channel is idle until the first push), but by uninstall time the operator may
   have deployed one via `dlp-ctl appcontrol apply`. A leftover WDAC enforcement policy after the
   agent is gone would keep blocking apps with no agent to manage it. Uninstall must strip it.

**Intended outcome:** the full agent installs on a clean Win11 Home VM with **no dev tools**, the
App Control channel sits idle (no policy) until the first push, on-endpoint `build` works out of the
box (ConfigCI auto-enabled), and **uninstall strips every App Control artifact** — deployed policy,
drop-folder tree, lists, and the status record — leaving the box exactly as before. This is the
final phase; afterwards all of AC-1…AC-5 are done.

## Locked decisions (this planning session)

| # | Decision | Rationale |
|---|---|---|
| D1 | **`enable_configci` is FAIL-CLOSED when `app_control.enabled` is true, keyed on a post-DISM PROBE — not per-package exit codes.** The servicing dir holds SEVERAL ConfigCI revisions (e.g. `26100.1591`/`.8246`/`.8521`); only the one matching the current cumulative-update level installs, the rest fail "not applicable" (DISM rc `14107`). So per-package DISM failures are EXPECTED and non-fatal — the loop continues past them (mirroring AC-1's PowerShell `gci \| %`, which never stopped on a failed `dism`). The step then **probes** `ConvertFrom-CIPolicy`; if it still does not resolve, the step **raises** → the driver rolls the whole install back. Idempotent (skips the loop if ConfigCI is already available — reinstall). **Escape hatch:** `app_control.enabled: false` skips the step entirely. | **AMENDED after the first VM run** (rc 14107 on a down-level .mum). User-chose fail-closed; the probe makes "failure" mean "ConfigCI genuinely unavailable", not "a redundant package didn't apply". Still guarantees a successful install has a working on-endpoint `build`; `enabled:false` + the `pre-AC5` snapshot + clean rollback mitigate the abort risk. |
| D2 | **Per-package DISM codes `0`/`3010` count as "added"; anything else counts as "not applicable" and is logged + skipped (not fatal).** `3010` = `ERROR_SUCCESS_REBOOT_REQUIRED` (web-verified). The added/failed split is reported in the step payload for diagnosability; the *step's* success is decided solely by the D1 probe. | Down-level revisions are normal in a serviced image; only the probe is authoritative. |
| D3 | **Step ordering resolves parent open-question #4.** `appcontrol_policy_guard` is placed **immediately before `install_service`** (last). Because undos run in **reverse**, its undo runs *after* `install_service`'s undo (service stopped) and *before* `copy_payload`'s undo (install tree, incl. the bundled `neutralizer.cip` / `base.xml`, still present). `appcontrol_dirs` + `enable_configci` are placed **early** (right after `add_to_path`) so a fail-closed ConfigCI error aborts *before* the heavier CA / proxy / shell-ext / service steps run. | Satisfies the parent's two constraints verbatim ("policy removal after service stop, while bundled Python still exists") and fails fast. |
| D4 | **`enable_configci` and `appcontrol_policy_guard` expose injectable runner seams** (`dism_runner=` / `citool_runner=`), exactly like `deployer.Deployer(runner=…)`. `_build_default_steps()` calls them with defaults (real DISM / real `citool`); the harness passes fakes. | Lets the new steps be unit-tested with zero OS side effects — the AC-2/AC-3/AC-4 testing discipline. |
| D5 | **`package-bundle.ps1` needs no code change.** It robocopies `orchestrator/` wholesale (so `orchestrator/app_control/` incl. `base.xml` + `neutralizer.cip` ships for free — none are `*.pyc`/`__pycache__`), and `build_bundle_config` copies the `app_control:` config section verbatim (AC-3 V6 already proved this). AC-5 only *confirms* it. | Reuse, not reinvention. |
| D6 | **Uninstall cleanup split:** `appcontrol_dirs.undo` removes the whole `%ProgramData%\DLP\appcontrol\` tree (lists + pushes + rejected); `appcontrol_policy_guard.undo` removes the deployed policy **and** unlinks `%ProgramData%\DLP\state\appcontrol_status.json`. Together they satisfy decision 7. | Clean separation: the dirs-step owns the drop-folder + lists; the guard-step owns the deployed-policy lifecycle (citool + status record). |

Inherited: parent decisions 1 (on-endpoint compile via ConfigCI — now automated here), 2
(pre-compiled `.cip` to the watcher), 3 (self-protect validate-and-reject), 4 (`deployer.remove()`
= `citool --remove-policy` + neutralizer fallback), 7 (idle until first push). **AC-2 carry-forward:**
every built policy already includes the `<install_root>\*` **and** `C:\Program Files\dotnet\*`
self-protect FilePath rules (the framework-dependent .NET runtime is NOT trusted by `base.xml`, so
without the dotnet rule the C# interceptors get blocked) — satisfied automatically by
`selfprotect.add_selfprotect_rules`. The installer keeps `<install_root>` = `%ProgramFiles%\DLP`,
which is admin-only-writable by default, so the FilePath self-protect rules stay honored — **no
explicit ACL step is needed** (verified: `mkdir` under `%ProgramFiles%` inherits its admin-write /
user-read ACL).

## Architecture — the amended install step list

`_build_default_steps()` (in `installer.py`) returns an ordered list of `Step(id, do, undo)` pairs.
`_drive_install` runs every `do` forward (persisting an undo payload to `install_manifest.json`
after each); on any exception it runs the completed steps' `undo`s in **reverse**. `_drive_uninstall`
runs every `undo` in reverse, swallowing "already absent" errors. **New steps in bold:**

```
require_admin
check_arch
verify_artifacts
make_dirs
copy_payload              # ships orchestrator/app_control (base.xml + neutralizer.cip) into install tree
install_ctl_wrapper
add_to_path
**appcontrol_dirs**       # NEW  do: mkdir appcontrol root+{inbox,rejected,staging}   undo: rmtree the whole appcontrol tree
**enable_configci**       # NEW  do: DISM ConfigCI .mum loop (fail-closed; skipped if app_control disabled)   undo: no-op
bootstrap_ca
install_root_ca
backup_proxy
set_proxy
register_shellext
notify_shell
**appcontrol_policy_guard** # NEW  do: record base PolicyID in manifest   undo: deployer.remove() + unlink appcontrol_status.json
install_service           # starts the service last
```

Resulting **uninstall** order (reverse): `install_service` (stop+delete) → **`appcontrol_policy_guard`**
(remove policy; service already stopped ✓, install tree still present ✓) → `notify_shell` →
`register_shellext` → `set_proxy` → `backup_proxy` → `install_root_ca` → `bootstrap_ca` →
**`enable_configci`** (no-op — leaving ConfigCI enabled is benign) → **`appcontrol_dirs`** (rmtree
appcontrol tree; service stopped so the watcher holds no locks) → `add_to_path` → `install_ctl_wrapper`
→ `copy_payload` (rmtree install tree) → `make_dirs`.

**Reuse map (no behavior change to these):** `orchestrator/app_control/paths.py`
(`appcontrol_root`/`inbox_dir`/`rejected_dir`/`staging_dir`/`status_path`), `policy_xml.py`
(`load_base_policy`/`get_policy_id` → the braced GUID `{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}`),
`deployer.Deployer.remove()` (citool remove + neutralizer fallback, never raises — returns bool),
`hashing._DISM_HINT` (the actionable DISM-enable hint string), `installer._rmtree_with_retry`
(locked-file-tolerant tree delete), `installer._noop_undo`.

## Implementation tasks (isolated, sequential)

### AC5-T1 — `_step_appcontrol_dirs()` in `installer.py`
- **do:** resolve `root = paths.appcontrol_root(ctx.config)` and the three sub-dirs via
  `paths.{inbox,rejected,staging}_dir(ctx.config)`; `mkdir(parents=True, exist_ok=True)` each that's
  absent; return `{"root": str(root), "created": [...]}`.
- **undo:** `_rmtree_with_retry(Path(payload["root"] or paths.appcontrol_root(ctx.config)))` —
  removes the **entire** appcontrol tree (inbox/rejected/staging + `allow-list.txt`/`deny-list.txt`
  + any pending/rejected pushes), per decision 7 + D6. Robust to a `None` payload (synthesized-sweep
  uninstall) by recomputing the root from config. (Caveat: if the operator overrode inbox/rejected/
  staging to dirs *outside* the appcontrol root — non-default — those externals aren't swept; default
  config keeps them under the root. Documented, not handled.)
- **Acceptance (unit):** `do` creates the four dirs under a per-test tmp `%PROGRAMDATA%`; `undo`
  removes the whole tree; second `undo` is a no-op (no error).

### AC5-T2 — `_step_enable_configci(dism_runner=None, packages_dir=None)` + `_default_dism_runner` in `installer.py`
- `_default_dism_runner(mum_path) -> (rc, combined_output)`: shells out to
  `<SystemRoot>\System32\Dism.exe /online /norestart /add-package:<mum_path>` (absolute path,
  falling back to bare `dism` if the file is missing — mirrors `deployer.citool_path()`); returns
  `(returncode, stdout+stderr)`. The AC-1-proven argv form.
- **do:**
  1. If `not ctx.config.app_control_enabled`: log "skipping ConfigCI (app_control disabled)" and
     return `{"skipped": True}` (D1 escape hatch — runner never called).
  2. `pkg_dir = Path(packages_dir or f"{SystemRoot}\\servicing\\Packages")`;
     `mums = sorted(pkg_dir.glob("*ConfigCI*.mum"))`.
  3. If `not mums`: **raise** `RuntimeError` (fail-closed, D1) with `_DISM_HINT` (the actionable
     manual recipe) + the "set `app_control.enabled: false` to skip" guidance.
  4. For each `mum`: `rc, out = (dism_runner or _default_dism_runner)(str(mum))`; if
     `rc not in (0, 3010)` (D2): **raise** `RuntimeError(f"dism /add-package failed for {mum.name} (rc={rc}): {out[:300]}. {_DISM_HINT}")`.
  5. Return `{"packages": [m.name for m in mums]}`.
- **undo:** `_noop_undo` (parent decision: leaving ConfigCI enabled is benign; recorded in manifest
  for replay completeness). Define a module-level `_DISM_HINT` mirroring `hashing._DISM_HINT`
  (one short string — installer is a different layer; avoids a private cross-module import).
- **Acceptance (unit):** (a) fake runner returning `(0,"")` over a tmp `packages_dir` seeded with
  two fake `*ConfigCI*.mum` files → returns `packages` of length 2, runner called per file;
  (b) `(3010,"")` → no raise; (c) `(1,"err")` → raises (fail-closed); (d) empty `packages_dir`
  → raises; (e) `config.app_control_enabled=False` → returns `{"skipped": True}`, runner **not**
  called.

### AC5-T3 — `_step_appcontrol_policy_guard(citool_runner=None)` in `installer.py`
- **do:** `from orchestrator.app_control import policy_xml as px`;
  `return {"policy_id": px.get_policy_id(px.load_base_policy())}` (the braced base GUID). Pure record
  — install deploys no policy (decision 7).
- **undo:** resolve `policy_id = (payload or {}).get("policy_id") or px.get_policy_id(px.load_base_policy())`
  (robust to `None` payload); `status_path = ctx.state_dir / "appcontrol_status.json"`; build
  `Deployer(status_path=status_path, policy_id=policy_id, runner=citool_runner)` and call
  `.remove()` (returns bool, never raises — it internally tries `citool --remove-policy` then the
  neutralizer fallback). Then `status_path.unlink(missing_ok-style)` (swallow `FileNotFoundError`).
  Net effect: a deployed enforcement policy is stripped at uninstall, and the status record is gone
  (D6). On a clean box (no policy) `remove()` is a benign no-op.
- **Acceptance (unit):** (a) `do` returns `{"policy_id": "{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}"}`;
  (b) seed a fake `appcontrol_status.json` under `ctx.state_dir`, inject a fake citool runner that
  reports the policy gone after `--remove-policy` → `undo` calls the runner with `--remove-policy`
  and unlinks the status file; (c) `undo` with `payload=None` still resolves the GUID from `base.xml`.

### AC5-T4 — wire the three steps into `_build_default_steps()`
- Insert `_step_appcontrol_dirs()` and `_step_enable_configci()` between `_step_add_to_path()` and
  `_step_bootstrap_ca()`; insert `_step_appcontrol_policy_guard()` between `_step_notify_shell()` and
  `_step_install_service()` (D3). Defaults → real DISM / real citool at install time.
- **Acceptance (unit):** assert the returned step `id`s contain the three new ids in the D3-mandated
  relative order: `appcontrol_dirs` and `enable_configci` both before `bootstrap_ca`;
  `appcontrol_policy_guard` after `copy_payload` and immediately before `install_service`.

### AC5-T5 — extend `scripts/harness/test_installer.py`
- Add the unit tests from T1–T4 (style mirrors the existing synthetic-step + `_minimal_context`
  pattern; use the conftest per-test `%PROGRAMDATA%`→tmp fixture for `appcontrol_dirs`, and
  `ctx.state_dir` (already a tmp path in `_minimal_context`) for the policy-guard status file).
- Add one regression assertion to `test_build_bundle_config_rewrites_for_vm`: include an
  `app_control: {enabled: true, poll_seconds: 3}` section in the source config and assert it survives
  verbatim in the bundle output (locks in D5).
- **Acceptance:** `pytest scripts/harness/test_installer.py -v` green; full `pytest scripts/harness`
  stays green (baseline 106 passed / 3 skipped from AC-4 + the new cases).

### AC5-T6 — docs: README.md + bundle README touch (light)
- `README.md`: in the AC-4 `dlp-ctl appcontrol` section, **replace** the "build needs ConfigCI on the
  endpoint (AC-5 will auto-enable; until then the AC-1 DISM step is a prerequisite)" note with
  "the installer auto-enables ConfigCI, so on-endpoint `build` works out of the box; set
  `app_control.enabled: false` to skip both the channel and the ConfigCI enable." Add a one-line
  uninstall note: "uninstall also removes any deployed App Control policy and the
  `%ProgramData%\DLP\appcontrol\` tree."
- `scripts/package-bundle.ps1` `README-DEPLOY.txt` heredoc: one line noting the App Control channel
  + that uninstall strips its policy/dirs. **No logic change to the script** (D5).
- **Acceptance:** docs read correctly; `package-bundle.ps1` still parses (PowerShell `-NoProfile`
  syntax check, dev-side).

> **No changes to** `__main__.py` (already dispatches `--install`/`--uninstall` →
> `run_install`/`run_uninstall`, verified at `orchestrator/__main__.py:45-49`), `config.py`/`config.yaml`
> (the `app_control:` section + defaulted fields already exist from AC-3; `app_control_enabled`
> defaults `True` at `config.py:50`), or any `orchestrator/app_control/*` module.

## Verification

**Discipline** (AC-2/AC-3/AC-4 practice): every **dev-side** step below I run and confirm green
**before** handoff. Per your decision, the side-effecting live paths (real DISM enable, real
`citool --remove-policy`) are exercised on dev **only via injected fake runners** (zero dev-state
mutation) plus a **read-only** `dism /online /get-packages` probe; their real execution happens in
the VM acceptance run, where the mechanics are already AC-1/AC-3/AC-4-proven.

### Dev-side (pre-tested green before handoff; repo root, `.venv`, Python 3.13)

| Step | Command | Pass check |
|---|---|---|
| **V1** new unit tests | `python -m pytest scripts/harness/test_installer.py -v` | all `PASSED`; covers T1–T4 (dirs create/undo; ConfigCI success/3010/fail-closed/empty/skip; policy-guard do+undo; step ordering) |
| **V2** full harness regression | `python -m pytest scripts/harness` | **0 failed**; only the 3 pre-existing elevation-gated admin-pipe tests `skipped` (baseline 106 passed / 3 skipped + new cases) |
| **V3** DISM argv + packages exist (read-only, no mutation) | `python -c "import os,glob; d=os.path.join(os.environ['SystemRoot'],'servicing','Packages'); print(len(glob.glob(os.path.join(d,'*ConfigCI*.mum'))))"` then `dism /online /get-packages | findstr /i ConfigCI` | glob count ≥ 1 (proves the install-time glob + path build are correct on this OS); `get-packages` lists ConfigCI packages (proves `dism.exe` works) — **read-only**, dev state unchanged |
| **V4** bundle config carries app_control | `python -c "from orchestrator.installer import build_bundle_config; build_bundle_config('config.yaml', r'tmp\\ac5\\config.yaml')"` then read `tmp\ac5\config.yaml` | `app_control:` present, `enabled: true` (D5) |
| **V5** package-bundle parses | `powershell -NoProfile -Command "$null = [ScriptBlock]::Create((Get-Content -Raw scripts\package-bundle.ps1)); 'ok'"` | prints `ok` (the README-DEPLOY.txt heredoc edit didn't break the script) |

> **Marked / cannot fully pre-test on dev (with reason):**
> - **Real DISM `/add-package`** — NOT run on dev. *Reason:* you chose stubbed-only on dev to keep the
>   dev box pristine; a fail-closed real DISM would mutate the dev component store. Pre-tested instead
>   via V1's injected fake runner (asserts the exact AC-1 argv + exit-code handling) + V3's read-only
>   probe. Real execution = VM step **S2** below, where AC-1 already proved the identical recipe adds
>   all ~12 `.mum` with no reboot on Home 26200.
> - **Real `citool --remove-policy`** — NOT run on dev. *Reason:* no DLP policy is deployed on dev, so
>   it would be a meaningless mutation. Pre-tested via V1's injected fake runner; real execution =
>   VM steps **S5/S7** (the AC-3/AC-4-proven `deployer.remove()` path).
> - **Packaging + the clean-VM run** — cannot run on the dev box (needs the C#/C++ build toolchain to
>   package, and a clean VM to install). The packaging commands are the unchanged AC-3/AC-4 ones; the
>   VM commands reuse AC-1/AC-3/AC-4-verified `citool`/`dlp-ctl` mechanics verbatim.

### VM-side — clean-VM acceptance (installed `DLPAgent` service, build 26200; user-executed)

`{GUID}` = `{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}`. `%AC%` = `C:\ProgramData\DLP\appcontrol`.
Every `citool` call wrapped `cmd /c "echo . | citool … --json"` (AC-1 stdin finding). Run
`dlp-ctl appcontrol …` from a **NEW elevated** shell (the installer added `%ProgramFiles%\DLP` to
PATH and dropped `dlp-ctl.cmd`). The service self-spawns its interceptor children and the App
Control channel threads; use `Restart-Service DLPAgent` + `dlp-ctl appcontrol status`, not manual
process launches, to drive it.

**Dev prep (on dev, before touching the VM):**
- Build artifacts + package the AC-5 bundle: `scripts\prepare-install-payload.ps1` (Developer
  PowerShell), `scripts\prepare-python-embed.ps1` (normal PowerShell), then
  `scripts\package-bundle.ps1` → `dist\DLP\` + `dist\DLP.zip`. Confirm the bundle carries App
  Control: `Get-ChildItem dist\DLP\orchestrator\app_control` shows `base.xml` + `neutralizer.cip`;
  `Select-String -Path dist\DLP\config.yaml -Pattern '^app_control:'` matches (D5 confirmation).

**Prerequisites (each has a check):**
- **P1** .NET 10 Desktop Runtime present (`dotnet --list-runtimes` → `Microsoft.WindowsDesktop.App 10.x`).
- **P2** VMware snapshot **`pre-AC5`** taken now (the top of the rollback ladder).
- **P3** ConfigCI is **NOT** pre-enabled on this VM (the whole point of AC-5): in Windows PowerShell
  5.1, `Get-Command ConvertFrom-CIPolicy` → *CommandNotFoundException*. *(If a prior phase's snapshot
  left it enabled, revert to a truly clean snapshot first — otherwise S2 can't prove the auto-enable.)*

| # | Action | Pass check | Rollback |
|---|---|---|---|
| **S0** clean baseline | `cmd /c "echo . \| citool --list-policies --json"` ; `Get-Command ConvertFrom-CIPolicy` | no DLP `{GUID}` in the policy list; ConvertFrom-CIPolicy **not found** (ConfigCI off) | — |
| **S1** install | right-click `install.cmd` → Run as administrator (or `python-embed\python.exe -m orchestrator --install --config config.yaml` elevated) | exit 0; `Get-Service DLPAgent` → Running | `uninstall.cmd` / snapshot |
| **S2** ConfigCI auto-enabled (the AC-5 deliverable) | in Windows PowerShell 5.1: `Get-Command ConvertFrom-CIPolicy` ; `dlp-agent.log` tail | now **resolves** (the `enable_configci` step ran); log shows "added N ConfigCI package(s)" | S8 |
| **S3** channel idle + dirs created | `dlp-ctl appcontrol status` ; `Test-Path $env:ProgramData\DLP\appcontrol\inbox,rejected,staging` | enabled, running, **no policy**, pending=0; all three dirs `True` | S8 |
| **S4** operator loop — author + build (on-VM compile now works) | `dlp-ctl appcontrol deny "C:\Program Files\Microsoft OneDrive\OneDrive.exe"` ; `dlp-ctl appcontrol allow "C:\Program Files\7-Zip"` ; `dlp-ctl appcontrol build` | lists updated; `build` runs `ConvertFrom-CIPolicy` (ConfigCI from S2) and writes `staging\build\{policy.xml,{GUID}.cip,manifest.json}`; report shows self-protect + version > deployed | S8 |
| **S5** apply → auto-deploy → enforce | `dlp-ctl appcontrol apply` ; within ~poll_seconds `dlp-ctl appcontrol status` + `cmd /c "echo . \| citool --list-policies --json"` ; launch denied `OneDrive.exe` (blocked) + an allowed app (runs) | staging consumed; `{GUID}` `IsEnforced:true`; `events.jsonl` `deploy` + `block` lines; status block counters increment | S8 |
| **S6** decision-8 **full** acceptance (8a + 8b) | **8a:** copy an installed app's exes into a folder (e.g. `7zFM.exe`,`7z.dll` → `C:\tmp\copies\`), `dlp-ctl appcontrol allow C:\tmp\copies`, `build`, `apply`; relaunch the **originally-installed** app. **8b:** `dlp-ctl appcontrol allow C:\Users\<u>\Downloads\7zXXXX-x64.exe` (an *installer* exe), `build`; inspect the report | **8a:** the originally-installed app **runs** (rules built from *copies* govern the originals — PE metadata travels with a copy). **8b:** builder emits the **installer-like-name warning**, and (if applied) the installed `7zFM.exe` is **not** covered by allowing the installer exe (InternalName mismatch) — both documented as admin-workflow caveats | S8 |
| **S7** disable (live, over admin-pipe) | `dlp-ctl appcontrol disable` | `removed=true`; status → no policy; `{GUID}` gone from `--list-policies`; OneDrive runs again | S8 |
| **S8** uninstall strips everything **while a policy is enforced** (decision 7 + the Follow-up fix below) | re-`apply` a build first (so a policy is deployed at uninstall time), then run the bundle `uninstall.cmd` as admin (and, separately, prove `C:\Program Files\DLP\uninstall.cmd` also works) ; afterwards: `cmd /c "echo . \| citool --list-policies --json"` ; `Test-Path "%AC%"` ; `Test-Path "$env:ProgramData\DLP\state\appcontrol_status.json"` ; `Test-Path "$env:ProgramFiles\DLP"` ; `Get-Service DLPAgent` | uninstaller **launches under enforcement** (runs the installed, policy-allowed python); `{GUID}` **gone** (policy_guard.undo ran `citool --remove-policy`, no reboot) ; `%AC%` tree **gone** ; `appcontrol_status.json` **gone** ; install tree gone ; service deleted — baseline restored | S9 |
| **S9** reinstall succeeds (clean re-entry) | `install.cmd` as admin again | exit 0; `Get-Service DLPAgent` Running; S2/S3 pass again (ConfigCI still enabled — its undo is a no-op, harmless) | snapshot `pre-AC5` |

**Passes when:** S2 proves the installer auto-enabled ConfigCI on a clean Home box, S4–S5 run the
full on-endpoint build→apply→enforce loop with **no manual DISM prerequisite**, S6 formally
re-demonstrates decision-8 (8a + 8b) in the installed-agent context, S8 strips **every** App Control
artifact at uninstall (policy + dirs + status record), and S9 reinstalls cleanly.

**Rollback ladder (AC-1/AC-3/AC-4-proven, no reboot):** `dlp-ctl appcontrol disable --force-local`
→ `cmd /c "echo . | citool --remove-policy ""{GUID}"" --json"` → deploy the shipped
`neutralizer.cip` + `citool --refresh` (immediate allow-all) → revert to snapshot **`pre-AC5`**.

## Files

- **Edit:** `orchestrator/installer.py` (three new `_step_*` factories + `_default_dism_runner` +
  `_DISM_HINT`; wire all three into `_build_default_steps()`).
- **Edit:** `scripts/harness/test_installer.py` (T1–T4 unit tests + the bundle-config app_control
  regression assertion).
- **Edit (docs only):** `README.md` (ConfigCI auto-enable + uninstall-strips note),
  `scripts/package-bundle.ps1` (one README-DEPLOY.txt line; **no logic change**).
- **Unchanged (reused):** `orchestrator/app_control/{paths,policy_xml,deployer,hashing,neutralizer,
  selfprotect,manifest,builder,channel}.py`, `orchestrator/__main__.py`, `orchestrator/config.py`,
  `config.yaml`.

## Web-verified facts (this session)

- **DISM exit code `3010`** = `ERROR_SUCCESS_REBOOT_REQUIRED` — installed OK, reboot pending; must be
  treated as success alongside `0` ([TechNet: exit code 3010](https://social.technet.microsoft.com/Forums/en-US/7f33d014-66ce-4bbe-a100-332a3bcbdd5f/exit-code-3010-suddenly-considered-as-failure), [DismAddPackage function](https://learn.microsoft.com/fi-fi/windows-hardware/manufacture/desktop/dism/dismaddpackage-function?view=windows-11)).
- **ConfigCI `.mum` offline-enable via DISM** is the supported approach on Win11 Home 23H2+ —
  `gci $Env:SystemRoot\servicing\Packages\*ConfigCI*.mum | % { dism /online /norestart /add-package:"$($_.FullName)" }`
  (matches AC-1's proven recipe) ([valinet/ssde#9](https://github.com/valinet/ssde/issues/9), [DISM package-servicing options](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/dism-operating-system-package-servicing-command-line-options?view=windows-11)).
- `ConvertFrom-CIPolicy` / `New-CIPolicy` are **current** ConfigCI cmdlets (Win Server 2025 PS docs) —
  not deprecated; ConfigCI provides them only when the module is enabled ([ConvertFrom-CIPolicy](https://learn.microsoft.com/en-us/powershell/module/configci/convertfrom-cipolicy?view=windowsserver2025-ps)).
- `citool --remove-policy` removes unsigned policies **without reboot** on Win11 24H2+ (the VM is
  26200) — AC-1 VM-proven and prior-phase web-verified.

## Parent-plan edits to apply on completion (mirroring AC-2/AC-3/AC-4 close-out)
- Mark `### Phase AC-5` ✅ COMPLETED with an Outcome paragraph (steps shipped, harness count,
  clean-VM acceptance result incl. decision-8 8a/8b).
- Strike cross-cutting **open question #4** (resolved by D3: `appcontrol_policy_guard` placed
  immediately before `install_service`).
- Note the fail-closed ConfigCI-enable decision (D1) + the `app_control.enabled:false` escape hatch.
  (Superseded: `package-bundle.ps1` *does* now get a logic change — the uninstall-bootstrap fix below.)
- With AC-5 done, mark the whole App Control channel integration **complete (AC-1…AC-5)**.

---

## AC-5 Follow-up — uninstall-under-enforcement fix (the S8 bug)

### Context
The first VM acceptance run failed at **S8**: uninstalling **while an enforcement policy is deployed**
hung/failed. Root cause (confirmed by reading `selfprotect.py`): the self-protect policy allows only
`<install_root>\*` (`C:\Program Files\DLP\*`) + `C:\Program Files\dotnet\*` (+ Microsoft-signed code
via `base.xml`). The bundle's embed `python.exe` is **PSF-signed and lives on the shared-folder bundle
path**, so it's covered by **neither** — under enforcement WDAC blocks it from launching. Both bundle
`.cmd`s prefer that bundle python, so `uninstall.cmd` couldn't even start. The **installed** python at
`C:\Program Files\DLP\python\python.exe` **is** covered by the FilePath self-protect rule, so running
the uninstaller from it is WDAC-allowed.

Verified enablers (read from the code): the embed's `python313._pth` contains `..`, which puts the
install root on `sys.path` from **any** cwd — so `<install_root>\python\python.exe -m orchestrator`
finds the orchestrator package regardless of where it's launched. And `_rmtree_with_retry` already
renames a running, locked `.exe` aside (the agent *already* needs a reboot remnant at uninstall for the
injected `Payload.dll`/`DlpShellExt.dll`), so running the uninstaller from the installed python adds
`python\` to that existing move-aside set — **no new reboot requirement**.

### Decisions (this session)
| # | Decision |
|---|---|
| F-D1 | **Uninstall must run from the installed (WDAC-allowed) python.** Flip the bundle `uninstall.cmd` to prefer `%ProgramFiles%\DLP\python\python.exe`, falling back to the bundle embed python only when the agent isn't installed (in which case no self-protect policy is enforcing). |
| F-D2 | **Ship an installed uninstaller** at `<install_root>\uninstall.cmd` (proposal 2) so uninstall works even if the bundle is gone. It **self-relaunches from `%TEMP%`** (copy self → `start` the temp copy → original exits immediately) so the running script is never inside the tree it deletes — no cosmetic "batch file cannot be found". Its cwd is set to `%SystemRoot%` (outside the tree, so the tree is removable); module resolution rides the `_pth` `..`. |
| F-D3 | **`install.cmd` stays bundle-python-only** (user-chosen). A re-install via the installed python can't overwrite its own running `python.exe` during `copy_payload`, and the bundle python is blocked under a live policy — so "re-install over a live policy" is awkward either way and is handled by *disabling the policy first* (documented). The normal uninstall→reinstall flow never hits this. |
| F-D4 | **`dlp-ctl.cmd` is unchanged** — it already prefers the installed python (WDAC-allowed), which is also the immediate recovery tool for a stuck VM (`dlp-ctl appcontrol disable`). |

### Implementation tasks (sequential)

**F1 — bundle `uninstall.cmd`: prefer the installed python** (`scripts/package-bundle.ps1`, the `$UninstallCmd` heredoc). Flip the existing `if exist … python-embed` / else branches so the **installed** python is tried first:
```bat
setlocal
set "PF=%ProgramFiles%\DLP"
REM Prefer the INSTALLED python: it is covered by the App Control self-protect policy
REM (C:\Program Files\DLP\*), so it launches even while an enforcement policy is
REM deployed. The bundle embed python is NOT covered and WDAC would block it. Fall
REM back to the bundle python only when the agent isn't installed (no policy enforcing).
if exist "%PF%\python\python.exe" (
  "%PF%\python\python.exe" -m orchestrator --uninstall --config "%PF%\config.yaml"
) else (
  "%~dp0python-embed\python.exe" -m orchestrator --uninstall --config "%~dp0config.yaml"
)
echo.
pause
```
(The bundle `.cmd` lives outside the install tree, so it has no self-deletion problem; its cwd = bundle dir is outside `C:\Program Files\DLP`, so the tree is removable.)

**F2 — installed uninstaller** (`orchestrator/installer.py`): new `_UNINSTALL_WRAPPER_BODY` template + `_step_install_uninstall_wrapper()` (mirrors `_step_install_ctl_wrapper`), wired into `_build_default_steps()` immediately after `_step_install_ctl_wrapper()`. `do` writes `<install_root>\uninstall.cmd` with the **absolute install root baked in** (not `%~dp0`, since after the `%TEMP%` relaunch `%~dp0`=`%TEMP%`); `undo` unlinks it (idempotent; `copy_payload.undo` would also remove it). Body:
```bat
@echo off
REM DLP Agent uninstaller (installed copy). Run as administrator.
REM Uses the INSTALLED python, which the App Control self-protect policy allows, so
REM uninstall works even while an enforcement policy is deployed. Re-launches from
REM %TEMP% so it never deletes the script that is running.
setlocal
if /i "%~1"=="_fromtemp" goto work
copy /y "%~f0" "%TEMP%\dlp-uninstall.cmd" >nul 2>&1
start "DLP Uninstall" "%TEMP%\dlp-uninstall.cmd" _fromtemp
exit /b 0
:work
cd /d "%SystemRoot%"
"{install_root}\python\python.exe" -m orchestrator --uninstall --config "{install_root}\config.yaml"
echo.
echo DLP uninstall finished.
pause
(goto) 2>nul & del "%~f0"
```
`{install_root}` is `str(ctx.install_root)` (the body has no other literal `{`/`}`, so `.format()` is safe). The `start` keeps the new console at the same elevation; `(goto) 2>nul & del "%~f0"` removes the temp copy after `pause`.

**F3 — tests** (`scripts/harness/test_installer.py`): a `test_install_uninstall_wrapper_writes_and_undoes` mirroring the ctl-wrapper test — `do` writes `<install_root>\uninstall.cmd` containing the baked install-root python path, `_fromtemp`, `start`, `%TEMP%`, and `-m orchestrator --uninstall`; `undo` unlinks; second `undo` is a no-op. Extend `test_build_default_steps_orders_appcontrol_steps_correctly` to assert `install_uninstall_wrapper` sits right after `install_ctl_wrapper`. (The `.cmd`'s *runtime* uninstall is **not** run on dev — it would attempt a real uninstall of `C:\Program Files\DLP` + service/cert/registry removal; VM-only. The step is verified file-write-only on dev.)

**F4 — docs** (`README.md` + `package-bundle.ps1` `README-DEPLOY.txt`): note that uninstall runs from the installed (policy-allowed) python; that `C:\Program Files\DLP\uninstall.cmd` is an installed uninstaller usable when the bundle is gone; and "disable the App Control policy (`dlp-ctl appcontrol disable`) before re-installing over a live one."

### Verification

**Dev-side (pre-tested green before handoff):**
| Step | Command | Pass check |
|---|---|---|
| FV1 wrapper unit test | `python -m pytest scripts/harness/test_installer.py -v` | new `install_uninstall_wrapper` test + ordering assertion PASS; all installer tests green |
| FV2 full harness | `python -m pytest scripts/harness` | 0 failed (only the 3 known elevation skips) |
| FV3 package-bundle parses | `powershell -NoProfile -Command "$null=[ScriptBlock]::Create((Get-Content -Raw scripts\package-bundle.ps1)); 'ok'"` | `ok` (the flipped `$UninstallCmd` heredoc is valid) |

> Not pre-testable on dev (marked): the actual uninstall behavior of the installed `.cmd` and the
> bundle `.cmd` under a live policy — running them on dev would attempt a real uninstall + WDAC ops on
> the dev box. VM-only; mechanics (citool remove, `_rmtree_with_retry` move-aside) are AC-1/AC-3-proven.

**VM — recover the currently-stuck VM first** (agent still installed, policy still enforcing because the
old `uninstall.cmd` was blocked): run the WDAC-allowed installed CLI to drop the policy, then proceed —
`dlp-ctl appcontrol disable` (or `dlp-ctl appcontrol disable --force-local`); verify `{GUID}` gone via
`cmd /c "echo . | citool --list-policies --json"`. (If `dlp-ctl` itself won't run, the manual escape is
`cmd /c "echo . | citool --remove-policy ""{GUID}"" --json"` — citool is Microsoft-signed, always allowed.)

**VM — re-run S8 after the fix** (rebuild the bundle: `scripts\package-bundle.ps1` regenerates
`uninstall.cmd` *and* copies the updated `orchestrator\`):
1. Install (S1), apply a policy (S4–S5) so enforcement is live.
2. **S8a:** run the **bundle** `uninstall.cmd` as admin → it launches (installed python, allowed), removes the policy + tree; verify the S8 pass-checks.
3. **S8b (bundle-gone case):** reinstall + re-apply a policy, then run `C:\Program Files\DLP\uninstall.cmd` as admin → a new "DLP Uninstall" window appears, completes with no "batch file cannot be found"; same S8 pass-checks; the `%TEMP%\dlp-uninstall.cmd` is gone afterward.

### Files (follow-up)
- **Edit:** `orchestrator/installer.py` (`_UNINSTALL_WRAPPER_BODY` + `_step_install_uninstall_wrapper` + wire into `_build_default_steps`), `scripts/package-bundle.ps1` (`$UninstallCmd` preference flip + README-DEPLOY note), `scripts/harness/test_installer.py` (wrapper test + ordering), `README.md` (uninstall/recovery notes).
- **Unchanged:** `install.cmd` (F-D3), `dlp-ctl.cmd` (F-D4), all `orchestrator/app_control/*`.
