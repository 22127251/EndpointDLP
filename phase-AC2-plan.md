# Phase AC-2 — Python WDAC Policy Engine (pure logic, harness-testable): Detailed Plan

## Status — ✅ COMPLETE (2026-06-11)

All tasks AC2-T1..T10 done. Dev gates green: full harness **69 passed / 3 skipped** (V4); real
`New-CIPolicyRule -Level Hash` → 4 hashes (V5); every generated policy (FileAttrib + FilePath
self-protect + Hash) compiles clean with `ConvertFrom-CIPolicy` (V6). **VM self-protect smoke PASSED
as expected** — Policy B (install_root FilePath only) left the .NET interceptor (controller child)
blocked with a `…\dotnet\…` 3077; Policy C (install_root + dotnet FilePath) ran it; clean removal back
to baseline. Shipped `orchestrator/app_control/{__init__,policy_xml,hashing,selfprotect,manifest}.py`
+ `base.xml`, `scripts/harness/test_app_control_policy.py` (34 cases), `scripts/build-ac2-policies.py`.
**AC-3 is unblocked.**

## Context

The App Control (WDAC) channel is being integrated into the DLP agent in phases
(`app-control-integration-plan.md`). **AC-1 is complete** — a VM spike proved every OS-level recipe
(DISM ConfigCI enable on Win11 Home 26200, on-endpoint compile, deploy/refresh/remove,
deny-beats-Store, live SYSTEM event capture) and pinned the contracts in
`interceptors/app_control/spike-results/RESULTS.md`.

**AC-2's job** is the *pure-logic core*: a Python WDAC policy engine that does all XML rule
manipulation + manifest validation **with no OS side effects** (one isolated, mockable exception —
`hashing.py`), fully covered by the existing pytest harness. It ports
`interceptors/app_control/cli/Add-WDACRule.ps1` to Python and adds what the runtime (AC-3) and the
operator CLI (AC-4) will call: rule insertion, self-protect rule generation, and the inbox-manifest
validator suite.

Why pure-logic-first: the self-protect *validator* (parent decision 3 — the agent refuses to deploy
a pushed policy that doesn't cover its own binaries) must be Python regardless, so porting the
rule-shape logic avoids maintaining it in two languages and makes it unit-testable. PowerShell stays
only where there is no Python equivalent (`ConvertFrom-CIPolicy` compile in AC-4; `New-CIPolicyRule
-Level Hash` for the rare hash fallback).

This plan covers **AC-2 only**. The in-orchestrator watcher/deployer/event-forwarder (AC-3),
`dlp-ctl` workflow (AC-4), and installer wiring (AC-5) are separate phases. AC-2 ships importable
modules + tests and **modifies no orchestrator runtime, no `config.py`, no installer**.

---

## Self-protect coverage — what the installed agent loads under UMCI, and how each is allowed

When the agent runs under its own enforced WDAC policy, **every user-mode EXE/DLL its process tree
loads must be allowed** or the agent breaks. Self-protect is built from **WDAC FilePath rules** on
admin-only-writable directories (single recursive rule per tree — fast, no per-file hashing, robust
to dependency updates), plus reliance on `base.xml`'s Windows-signer allow for in-box System32 tools.

| # | Agent dependency | Location | Allowed by |
|---|---|---|---|
| 1 | Embedded CPython (`python.exe`, `python3*.dll`, `vcruntime140*.dll`, stdlib `.pyd`) | `<install_root>\python\` | **install_root FilePath** ✓ |
| 2 | Orchestrator + analyzer `.py` and **native analyzer wheels** (pyahocorasick, google-re2, PyMuPDF, pywin32, PyYAML `_yaml`, …) — several with **empty PE version info** | `<install_root>\python\Lib\site-packages\`, `<install_root>\orchestrator\`, `analyzer\` | **install_root FilePath** ✓ (this is precisely why FilePath beats per-file FileAttrib/Hash for self-protect) |
| 3 | mitmdump (launched as `python.exe -c` shim, Phase E PF#5) | `<install_root>\python\` | **install_root FilePath** ✓ |
| 4 | C# interceptor apphosts (`UsbDlpController.exe`, `ClipboardInterceptor.exe`, `DlpTransferAgent.exe`) | `<install_root>\bin\…` | **install_root FilePath** ✓ |
| 5 | Native C++ DLLs (`Payload.dll` injected into `explorer.exe`; `DlpShellExt.dll` loaded by `explorer.exe`) | `<install_root>\bin\…` | **install_root FilePath** ✓ (WDAC checks the DLL's on-disk path regardless of host process) |
| 6 | **.NET 10 shared runtime** (`hostfxr.dll`, `hostpolicy.dll`, `Microsoft.NETCore.App\*`, `Microsoft.WindowsDesktop.App\*`) — the C# apps are **framework-dependent** | `C:\Program Files\dotnet\` (**outside** install_root) | **NEW: dotnet FilePath rule** `C:\Program Files\dotnet\*` |
| 7 | Windows PowerShell + ConfigCI module (compile, `New-CIPolicyRule`) | `%WINDIR%\System32\WindowsPowerShell\v1.0\…` + module path | `base.xml` Windows-signer allow + `Disabled:Script Enforcement` (ran under enforced P1/P2 in AC-1) |
| 8 | `citool`, `certutil`, `sc`, `reg`, `dism` (deploy + install tooling) | `%WINDIR%\System32\` | `base.xml` Windows-signer allow (ran under enforced P1/P2 in AC-1) |
| 9 | Config/state/inbox data + `.cip` files | `%ProgramData%\DLP\…`, `System32\CodeIntegrity\…` | data, not executed PE → **no rule needed** |

**Row 6 is the correction to the earlier draft and is web-verified, not inferred.** A
DefaultWindows-style base (Windows/WHQL/Store EKUs only — which `base.xml` is) does **not** trust the
.NET runtime: .NET binaries are signed with **Microsoft Code Signing PCA 2011** (a *dedicated* ".NET"
leaf cert since .NET 6, thumbprint `60ff375e5669b98d43ea0e2328e618cf73c0f91d`), **not** the Windows
EKU `1.3.6.1.4.1.311.10.3.6`, so they are blocked by design unless explicitly allowed. (The AC-1
OneDrive.exe / olk.exe blocks were **explicit P2 deny rules** added to force browser-routed uploads
through the agent's interceptor — they are *not* evidence about base coverage.) `C:\Program
Files\dotnet` is admin-only-writable, so a FilePath rule there is honored without enabling the weaker
`Disabled:Runtime FilePath Rule Protection` (option 18). This is **empirically confirmed in task
AC2-T10** before it is relied upon.

Self-protect therefore emits **two FilePath allow rules**: `<install_root>\*` and the dotnet runtime
root. The required-paths set is a parameter (default `[install_root, %ProgramFiles%\dotnet]`) so
AC-4/AC-5 can extend it via config; rows 7–8 are documented as base-covered and re-confirmed on the
VM in AC-5.

---

## Key decisions (locked this session)

| # | Decision |
|---|---|
| 1 | **Self-protect = FilePath rules** on `<install_root>\*` **and** `C:\Program Files\dotnet\*` (both admin-only-writable). No per-binary scan/hash for the agent's own files. The validator (decision 3) checks the pushed policy contains Allow FilePath rules covering **all** required paths, referenced in the UMCI scenario. |
| 2 | **.NET runtime needs an explicit allow** (web-verified DefaultWindows-base does not trust it). FilePath chosen over a publisher/signer rule for simplicity + robustness to runtime patch moves. |
| 3 | **Hash fallback shells out to `New-CIPolicyRule -Level Hash`** (user-approved). Needed only for the operator's arbitrary no-metadata files (AC-4), not for self-protect. A thin, **mockable** helper invokes it + parses the 4 hashes; the pure-Python XML code inserts `<Allow Hash=…>` + refs with values injected, so the AC-2 suite stays PowerShell-free. |
| 4 | **`base.xml` is copied** to `orchestrator/app_control/base.xml` as package data (canonical for the agent). The `interceptors/app_control/cli/` copy stays as the superseded standalone spike tool; not deleted. |
| 5 | **AC-2 reads `RESULTS.md` + `versioninfo/targets.csv`** (user-approved) for parity grounding (InternalName values, empty-version-info cases, generic-name hazard). |

---

## Implementation tasks (isolated + trackable)

Each task is independently implementable, testable, and reviewable. Tasks T2–T8 add code with unit
tests; T9 consolidates the suite; T10 is the on-VM empirical gate for the self-protect design.

### AC2-T1 — Package scaffold + relocate `base.xml`
- **Scope:** create `orchestrator/app_control/__init__.py`; copy `interceptors/app_control/cli/base.xml`
  → `orchestrator/app_control/base.xml` (byte copy).
- **Files:** `orchestrator/app_control/__init__.py`, `orchestrator/app_control/base.xml`.
- **Acceptance:** `import orchestrator.app_control` succeeds; the package `base.xml` is byte-identical
  to the cli copy.

### AC2-T2 — `policy_xml.py`: load / serialize / version / PolicyInfo
- **Scope:** `load_base_policy() -> ElementTree` (parse packaged `base.xml`);
  `serialize(doc, path)` (UTF-8 + XML decl, default namespace via
  `ET.register_namespace('', 'urn:schemas-microsoft-com:sipolicy')`, `ET.indent(space='  ')`);
  `set_version_ex(doc, version)` / `bump_version_ex(doc)` (4th field, PS1 parity);
  `set_policy_info_id(doc, value)` (`Settings/Setting[@ValueName='Id']/Value/String`).
- **Files:** `orchestrator/app_control/policy_xml.py`.
- **Acceptance:** unit test — load base, bump/set VersionEx, stamp Id, serialize; output re-parses
  with `ET.parse`, default namespace preserved (no `ns0:`), VersionEx/Id correct.

### AC2-T3 — `policy_xml.py`: FileAttrib rules (PS1 port core)
- **Scope:** `read_file_attribute(path, level="InternalName") -> str|None` via
  `win32api.GetFileVersionInfo` (translation `\VarFileInfo\Translation`[0] →
  `\StringFileInfo\{lang:04x}{cp:04x}\{level}`; returns `None`/`""` when absent);
  `add_file_attrib_rule(doc, level, value, *, allow=True)` — insert `<Allow>`/`<Deny>` into
  `<FileRules>` + `<FileRuleRef>` into UMCI `SigningScenario Value="12"` `ProductSigners/FileRulesRef`;
  `ID_ALLOW_A_n`/`ID_DENY_D_n` auto-numbering (scan existing max) with **dedup** (same level+value →
  reuse). FriendlyName mirrors PS1.
- **Files:** `orchestrator/app_control/policy_xml.py`.
- **Acceptance:** unit test (mock `read_file_attribute`) — allow+deny built; IDs continue past
  existing; FileRuleRefs wired into scenario 12; duplicate value → one rule.

### AC2-T4 — `policy_xml.py`: FilePath rules (new capability)
- **Scope:** `add_filepath_rule(doc, filepath, *, allow=True)` — insert
  `<Allow … FilePath="…\*" MinimumFileVersion="0.0.0.0" />` + FileRuleRef; same ID scheme + dedup +
  UMCI wiring.
- **Files:** `orchestrator/app_control/policy_xml.py`.
- **Acceptance:** unit test — `<Allow FilePath="…\*">` present with `MinimumFileVersion`, ref wired.

### AC2-T5 — `policy_xml.py`: Hash rules + risky-name warnings
- **Scope:** `add_hash_rules(doc, friendly, hashes, *, allow=True)` — insert the four `<Allow
  Hash=…>` (values injected) + four FileRuleRefs; `warn_on_risky_attribute(level, value) ->
  list[str]` — warn on installer-like names (`*setup*`, `*-x64`, version-bearing originalnames) and
  overly-generic values (e.g. OneDrive `Client Application`); logged + returned.
- **Files:** `orchestrator/app_control/policy_xml.py`.
- **Acceptance:** unit test — 4 hash elements + 4 refs; warnings fire on a generic and an
  installer-like value, none on a normal one.

### AC2-T6 — `hashing.py`: isolated `New-CIPolicyRule -Level Hash` shell-out
- **Scope:** `compute_wdac_hashes(file_path, *, runner=<default subprocess>) -> list[str]` — invoke
  `powershell -NoProfile -Command "$r = New-CIPolicyRule -Level Hash -DriverFiles (Get-DriverFile
  <path>); New-CIPolicy -Rules $r -FilePath <tmp> -UserPEs"` (exact incantation dry-run-verified at
  implementation), parse the four `<Allow Hash=…>` out of `<tmp>`, return them; `runner` injectable
  (fake in tests); ConfigCI preflight raises a clear DISM-enable hint if absent.
- **Files:** `orchestrator/app_control/hashing.py`.
- **Acceptance:** unit test with a fake runner returning a captured sample policy XML → returns the 4
  hashes. The real-runner path is dry-run-verified on dev (ConfigCI present per AC-1).

### AC2-T7 — `selfprotect.py`: FilePath self-coverage + validator
- **Scope:** `required_filepaths(install_root, *, dotnet_root=%ProgramFiles%\dotnet) ->
  list[str]` (returns the `…\*` wildcards); `add_selfprotect_rules(doc, install_root, **kw)` (adds an
  Allow FilePath rule for each required path via `policy_xml.add_filepath_rule`);
  `policy_covers_required_paths(doc, install_root, **kw) -> bool` (decision-3 validator core: every
  required FilePath present **and** referenced in UMCI FileRulesRef).
- **Files:** `orchestrator/app_control/selfprotect.py`.
- **Acceptance:** unit test — after `add_selfprotect_rules`, both install_root and dotnet rules
  present + referenced; validator True; base policy without them → False; only install_root (missing
  dotnet) → False; a rule for a *different* root → False.

### AC2-T8 — `manifest.py`: inbox manifest schema + validator suite
- **Scope:** `Manifest` dataclass + `parse_manifest(text|dict)` (strict);
  schema `{schema_version, policy_id, version_ex, created, source, files:{policy_xml:{name,sha256},
  cip:{name,sha256}}}`; `flat_sha256(path)` (plain file SHA-256 — transport integrity, **not** a WDAC
  hash); validators `validate_file_hashes`, `validate_cip_name_matches_policy_id`
  (`{policy_id}.cip`), `validate_version_greater(deployed_version_ex)`,
  `validate_selfprotect(policy_doc, install_root)` (→ `selfprotect.policy_covers_required_paths`);
  `validate_all(...) -> list[Failure]`. Pure; shared by AC-3 runtime + tests.
- **Files:** `orchestrator/app_control/manifest.py`.
- **Acceptance:** unit test — happy path passes; each rejection class fails distinctly (sha256
  mismatch, cip-name mismatch, stale/equal version, missing self-protect coverage).

### AC2-T9 — Test suite consolidation + regression gate
- **Scope:** `scripts/harness/test_app_control_policy.py` collecting all T2–T8 cases (pure-logic, no
  subprocess, no real PE files — mock `read_file_attribute` and the hashing `runner`); style mirrors
  `test_events.py`.
- **Files:** `scripts/harness/test_app_control_policy.py`.
- **Acceptance:** `python -m pytest scripts/harness/test_app_control_policy.py -v` passes; full
  `python -m pytest scripts/harness` stays green (baseline 35 passed / 3 skipped + 10 C#).

### AC2-T10 — Compile cross-check (dev) + self-protect VM verification (empirical gate)
- **Scope (dev, side-effect-free, never deploys):** V6 below — build base + self-protect FilePath
  (install_root + dotnet) + FileAttrib + injected-Hash policies via `policy_xml` and
  `ConvertFrom-CIPolicy` them on dev (ConfigCI present per AC-1); validates ElementTree serialization
  against the real WDAC schema. **Also produce the two enforce-mode `.cip` handed to the VM step:**
  Policy **B** = base + install_root FilePath only; Policy **C** = base + install_root + dotnet FilePath.
- **Scope (VM, empirical — confirms coverage-table row 6):** the procedure in **Verification →
  VM-side** below. The agent installs as a self-spawning service, so the test deploys Policy B then C
  against the **already-installed, running `DLPAgent` service** and forces a child re-spawn with
  `Restart-Service` (it does **not** launch `UsbDlpController.exe` by hand — the supervisor does), then
  reads child state from `dlp-ctl status` + `supervisor-controller.log` + CodeIntegrity 3077. Fully
  reversible (`citool --remove-policy`; VMware snapshot is the last rung).
- **Files:** none (verification only; reuses `scripts/spike-*` + the V6-built `.cip`).
- **Acceptance:** dev compile clean; on the VM, Policy B → the controller child stays **down** with a
  3077 referencing the `C:\Program Files\dotnet\…` runtime (install_root alone is insufficient);
  Policy C → the controller child is **Running** again; removal returns to the baseline (all children
  Running).

---

## Web-verified facts (this session)

- **pywin32 `win32api.GetFileVersionInfo`** reads StringFileInfo via `\VarFileInfo\Translation` →
  `\StringFileInfo\{lang:04x}{cp:04x}\{key}`; current, non-deprecated; faithful equivalent of the
  PS1's `[System.Diagnostics.FileVersionInfo]`. ([pywin32 docs](https://mhammond.github.io/pywin32/win32api__GetFileVersionInfo_meth.html))
- **WDAC FilePath rules**: `<Allow … FilePath="C:\…\*" MinimumFileVersion="0.0.0.0" />`; trailing `*`
  authorizes all EXE/DLL in the path **and subdirectories recursively**; EXE/DLL only (fine — agent
  code is EXE+DLL incl. `.pyd`). Honored only for **admin-only-writable** paths unless option 18 is
  set — `%ProgramFiles%` and `%ProgramFiles%\dotnet` qualify, so we do **not** set option 18.
  ([MS Learn: rule levels & filepath rules](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/design/select-types-of-rules-to-create), [airdesk: WDAC path rules](https://airdesk.com/2019/11/mdac-and-path-rules/))
- **.NET is NOT trusted by a DefaultWindows base**: such a base has only explicit Windows/WHQL/Store
  EKU allows, no allow-all; .NET runtime DLLs are signed with **Microsoft Code Signing PCA 2011** (a
  dedicated ".NET" leaf since .NET 6, thumbprint `60ff375e…`), **not** the Windows EKU
  `1.3.6.1.4.1.311.10.3.6`, so they are blocked unless explicitly allowed. ([dotnet/runtime #51967 — .NET signing cert split for WDAC](https://github.com/dotnet/runtime/issues/51967), [HotCakeX — EKUs in WDAC](https://github.com/HotCakeX/Harden-Windows-Security/wiki/EKUs-in-WDAC,-App-Control-for-Business,-Policies), [SpyNetGirl — App Control notes](https://spynetgirl.github.io/WDAC/WDAC%20Notes/)) — **re-confirmed empirically in AC2-T10**.
- **WDAC Hash rules** = four values/file (SHA1+SHA256 Authenticode, SHA1+SHA256 page hash); page
  hashes need PE-layout-aware hashing → shell out to `New-CIPolicyRule -Level Hash` for the rare
  no-metadata fallback. ([MS Learn: hashes](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/design/select-types-of-rules-to-create))
- **AC-1 pinned contracts** (`spike-results/RESULTS.md`): PS1 rule shapes compile on Home 26200 and
  enforce correctly; empty-version-info files exist in the wild (`pcre2-8.dll`); generic InternalName
  hazard (`Client Application`) → warn; the Python port passes no path lists on a command line,
  avoiding AC-1's `WinError 206` 32K-arg limit inherently.

---

## Verification

**Discipline (per your requirement):** every step below has an explicit **pass check**. I run each
**dev-side** step myself during implementation and confirm its pass signal **before** reporting the
task done. For the **VM-side** step (which only you can run), I first confirm all of its dev
prerequisites are green (the policy compiles, the exact `.cip` is produced) and I reuse `citool`
mechanics **verbatim from AC-1's already-VM-validated recipes** — so what I hand you is syntactically
checked and built on proven commands, with a stated expected result and a rollback for each line.
AC-2 adds **no build steps** (pure Python; no C#/C++/MSBuild).

### Dev-side steps — I run and confirm each is green before hand-off

| Step | Command (repo root, dev venv) | Pass check |
|---|---|---|
| **V1** Package imports | `python -c "import orchestrator.app_control"` | exit 0, no traceback |
| **V2** `base.xml` relocated intact | compare SHA-256 of `orchestrator/app_control/base.xml` vs `interceptors/app_control/cli/base.xml` | hashes equal |
| **V3** Module unit tests (run as each task T2–T8 lands) | `python -m pytest scripts/harness/test_app_control_policy.py -v` | every test `PASSED`, **0 failed**, 0 errors |
| **V4** Full harness regression | `python -m pytest scripts/harness` | **0 failed**; only the 3 pre-existing admin-pipe elevation tests `skipped`; prior count (35 passed) unchanged + new tests passed |
| **V5** `hashing.py` real-runner dry-run | call `compute_wdac_hashes` on a known signed exe (e.g. `C:\Windows\System32\notepad.exe`) | returns exactly **4 non-empty hex** strings; the `New-CIPolicyRule`/`New-CIPolicy` subprocess exits 0 |
| **V6** Dev compile cross-check (side-effect-free, **never deploys**) | build a representative policy via `policy_xml` (FileAttrib allow+deny + self-protect FilePath for install_root **and** dotnet + injected Hash) to `tmp\ac2\test.xml`, then `powershell -NoProfile -Command "ConvertFrom-CIPolicy -XmlFilePath tmp\ac2\test.xml -BinaryFilePath tmp\ac2\test.cip"` | command exits 0; `test.cip` produced; **no** schema/validation error |

If any dev step fails, I fix it and re-run before moving on; I do not report a task done on a red check.

### VM-side step (AC2-T10 self-protect verification) — handed over only after V6 is green

This step is grounded in how the agent actually runs (README §B): the `DLPAgent` **service**
(start=auto) **self-spawns** mitmdump / ClipboardInterceptor / **UsbDlpController** through its
supervisor — so we **never launch an interceptor by hand**; we deploy a policy, then force the service
to re-spawn its children **under** that policy with `Restart-Service`, and read child state from
`dlp-ctl status` + the supervisor log. The experiment isolates exactly the .NET/dotnet contribution by
comparing two policies that differ **only** by the dotnet FilePath rule:
- **Policy B** = base + `FilePath C:\Program Files\DLP\*` (install_root only), VersionEx `10.4.0.1`.
- **Policy C** = base + `FilePath C:\Program Files\DLP\*` + `FilePath C:\Program Files\dotnet\*`, VersionEx `10.4.0.2`.

Both compile to `{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}.cip` (shared PolicyID), so they are delivered
in **separate folders** and deployed one at a time. The VM needs **no** ConfigCI and no compiler — the
`.cip` are pre-built + compile-verified on dev (V6); the VM only runs built-in `citool` (elevated).
**Assumes a standard `C:\Program Files`** (the policy paths are literal); if the VM's `%ProgramFiles%`
differs, I rebuild the `.cip` with the actual path.

**Prerequisites (do all before S0; each has its own check):**
- **P1 — .NET 10 Desktop Runtime present** (README §B.1 #2): `dotnet --list-runtimes` shows a
  `Microsoft.WindowsDesktop.App 10.x` line.
- **P2 — agent installed and running** (README §B.2): right-click `install.cmd` → Run as administrator;
  `Get-Service DLPAgent` → **Running**. This creates `C:\Program Files\DLP\` (with `python\` +
  `bin\Controller\UsbDlpController.exe`) and confirms the .NET interceptors run with **no** WDAC policy.
- **P3 — VMware snapshot** taken now, named `pre-AC2-selfprotect` (last-rung rollback).
- **P4 — the two `.cip` copied to the VM** (e.g. via shared folder) to exactly:
  - `C:\ac2-test\B\{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}.cip`  (Policy B)
  - `C:\ac2-test\C\{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}.cip`  (Policy C)

**Steps — all in an elevated PowerShell; every `citool` call is wrapped `cmd /c "echo . | citool … --json"`
to satisfy AC-1's stdin-prompt finding; each step states its pass signal + rollback. `{GUID}` =
`{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}`.**

| # | Command(s) | Pass check | Rollback |
|---|---|---|---|
| **S0** baseline | `cmd /c "echo . \| citool --list-policies --json"` ; then `dlp-ctl status` | no DLP `{GUID}` listed; `dlp-ctl status` shows **controller** (+ mitmdump, clipboard) **Running** | — |
| **S1** deploy B | `Copy-Item C:\ac2-test\B\{GUID}.cip C:\Windows\System32\CodeIntegrity\CIPolicies\Active\` ; `cmd /c "echo . \| citool --refresh --json"` ; `cmd /c "echo . \| citool --list-policies --json"` | refresh → `{"OperationResult":0}`; list shows `{GUID}` `IsEnforced:true` `VersionString:"10.4.0.1"` | S4 (remove) |
| **S2** respawn under B | `Restart-Service DLPAgent` ; then **wait ~90 s** (past the supervisor's 3-crash/60 s give-up window) | service restarts (python is allowed by install_root → service itself comes up) | S4 |
| **S3** observe B | `dlp-ctl status` ; `Get-Content C:\ProgramData\DLP\logs\supervisor-controller.log -Tail 30` ; `Get-WinEvent -LogName 'Microsoft-Windows-CodeIntegrity/Operational' -MaxEvents 30 \| ? { $_.Id -eq 3077 }` | **controller child NOT running** (gave up); supervisor log shows repeated start→exit; a **3077** names `…\dotnet\…\hostfxr.dll` (or the controller's runtime DLL) → **install_root alone does not cover .NET** | S4 |
| **S4** remove B | `cmd /c "echo . \| citool --remove-policy ""{GUID}"" --json"` ; `cmd /c "echo . \| citool --list-policies --json"` ; `Restart-Service DLPAgent` | `Operation Successful`, **no reboot**; `{GUID}` gone; after restart `dlp-ctl status` → controller **Running** again (recovered) | snapshot revert |
| **S5** deploy C | `Copy-Item C:\ac2-test\C\{GUID}.cip C:\Windows\System32\CodeIntegrity\CIPolicies\Active\` ; `cmd /c "echo . \| citool --refresh --json"` ; `cmd /c "echo . \| citool --list-policies --json"` | refresh → `{"OperationResult":0}`; list shows `{GUID}` `VersionString:"10.4.0.2"` `IsEnforced:true` | S8 (remove) |
| **S6** respawn under C | `Restart-Service DLPAgent` ; **wait ~90 s** | service restarts | S8 |
| **S7** observe C | `dlp-ctl status` ; `Get-WinEvent … \| ? { $_.Id -eq 3077 }` (since the S5 deploy time) | **all children Running** (mitmdump + clipboard + **controller**); **no new** 3077 for the agent's runtime → the dotnet FilePath rule is **necessary + sufficient** | S8 |
| **S8** revert | `cmd /c "echo . \| citool --remove-policy ""{GUID}"" --json"` ; `cmd /c "echo . \| citool --list-policies --json"` ; `Restart-Service DLPAgent` | `{GUID}` gone, no reboot; list matches S0; `dlp-ctl status` → all children Running | snapshot `pre-AC2-selfprotect` |

**AC2-T10 passes when:** S3 shows the controller down with a `…\dotnet\…` 3077 (install_root alone
insufficient), S7 shows the controller Running again under Policy C (dotnet rule fixes it), and S8
returns to the S0 baseline. This empirically confirms coverage-table row 6 before the .NET self-protect
rule is relied upon in AC-3/AC-5.

**Rollback ladder (AC-1-proven, no reboot):** `citool --remove-policy "{GUID}"` → if a refresh ever
wedges, deploy an AllowAll neutralizer `.cip` + refresh → if still stuck, revert to snapshot
`pre-AC2-selfprotect`.

---

## Out of scope / downstream

- **AC-3**: inbox watcher, `citool` deployer, event forwarder, `config.py` `app_control:` section,
  `events.py` emitter, `__main__.py` wiring. AC-2 ships the manifest validators they call.
- **AC-4**: `dlp-ctl appcontrol allow|deny|build|apply|status|disable`, `builder.py` driving
  `policy_xml` + `hashing` + `ConvertFrom-CIPolicy`, admin-pipe routing. FileAttrib stays the default
  operator level; FilePath/Hash available via `policy_xml`.
- **AC-5**: installer dirs/DISM/policy-guard steps; clean-VM acceptance. **Carry-forward constraint:**
  the installer must keep `<install_root>` (and rely on `%ProgramFiles%\dotnet` staying)
  admin-only-writable — that is what makes the self-protect FilePath rules trustworthy.

## Done when

`orchestrator/app_control/{policy_xml,selfprotect,manifest,hashing}.py` + `__init__.py` + `base.xml`
exist; `test_app_control_policy.py` passes; the full harness stays green; a `base.xml`-derived policy
built by `policy_xml` (FileAttrib + self-protect FilePath for install_root **and** dotnet + injected
Hash) compiles clean with `ConvertFrom-CIPolicy` on dev; and the AC2-T10 VM check confirms the
installed `DLPAgent` service's .NET interceptor (the controller child) stays **down** under Policy B
(install_root only — with a `…\dotnet\…` 3077) and runs under Policy C (install_root + dotnet),
reverting cleanly to the baseline. No orchestrator runtime, config, or installer files changed.
**All met and VM-confirmed (2026-06-11).**

---

## General plan (`app-control-integration-plan.md`) — edits to apply on approval

Mark Phase AC-2 complete in the parent plan, mirroring how AC-1 was closed out. Four edits:

**Edit A — AC-2 heading → COMPLETED + Outcome.** Change the heading
`### Phase AC-2 — Python WDAC policy engine (pure logic, harness-testable)` to add
`✅ COMPLETED (2026-06-11)` and insert an **Outcome** paragraph before **Goal:** recording: shipped
modules + 34-case test suite + `scripts/build-ac2-policies.py`; harness 69 passed/3 skipped;
`New-CIPolicyRule -Level Hash` returns the 4 hashes; all generated policies compile via
`ConvertFrom-CIPolicy`; **self-protect redesigned to FilePath rules** (`<install_root>\*` +
`C:\Program Files\dotnet\*`); **web-verified + VM-confirmed the DefaultWindows base does not trust the
framework-dependent .NET runtime** (Microsoft-Code-Signing-PCA, not Windows EKU), so the dotnet
FilePath rule is mandatory (VM smoke: Policy B blocks the .NET interceptor with a `…\dotnet\…` 3077,
Policy C runs it); hash fallback resolved to the `New-CIPolicyRule` shell-out. Point to the detailed
plan file.

**Edit B — AC-2 body updates:**
- "New files" line → add `hashing.py` and the keeper `scripts/build-ac2-policies.py`.
- `selfprotect.py` bullet → replace "agent-binary allow rules from the install layout (python.exe,
  interceptor exes, …)" with: emits two **FilePath** allow rules (`<install_root>\*` +
  `C:\Program Files\dotnet\*`) + the decision-3 validator `policy_covers_required_paths`; FilePath on
  admin-only-writable dirs avoids mass-hashing native analyzer deps.
- The "Open question for the phase session" (Authenticode hash) → **RESOLVED:** shell out to
  `New-CIPolicyRule -Level Hash` (4 hashes/file incl. page hashes, too risky to reimplement); XML
  insert stays pure + unit-tested; self-protect avoids hashing entirely via FilePath.

**Edit C — cross-cutting open question #1 → RESOLVED.** Strike-through
`1. **AC-2:** Authenticode-hash-in-Python vs ...` and append
`**RESOLVED (AC-2): shell-out** — hashing.py invokes New-CIPolicyRule -Level Hash (isolated + mocked);
self-protect sidesteps hashing via FilePath rules.`

**Edit D — carry-forward to AC-3/AC-5.** Add a one-line note that the deployed/standalone-built policy
**must** include the dotnet FilePath self-protect rule (the .NET runtime is not base-trusted) and that
the AC-5 installer must keep `<install_root>` admin-only-writable so the FilePath rules stay honored.
