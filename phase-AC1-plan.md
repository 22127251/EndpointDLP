# Phase AC-1 — WDAC VM Semantics Spike: Detailed Plan

## Context

The app-control channel integration (`app-control-integration-plan.md`) starts with **Phase AC-1**: a de-risking spike that converts every OS-level assumption about WDAC (Windows Defender Application Control, aka "App Control for Business" — the Windows feature that blocks executables/DLLs not allowed by a deployed Code Integrity policy) into **verified command recipes + recorded artifacts**, using the *existing* CLI tool at `interceptors/app_control/cli/`, on the Win11 Home build 26200 test VM. **No agent code changes.** The recipes feed AC-3 (deployer/event-forwarder) and AC-5 (installer); the recorded event XML pins the field names AC-3's filter will parse.

Glossary used throughout: **UMCI/KMCI** = user-mode / kernel-mode code integrity (the policy's two enforcement scenarios); **`.cip`** = compiled binary policy (what Windows actually loads, produced from the XML by `ConvertFrom-CIPolicy`); **neutralizer** = an allow-everything policy update with the *same* PolicyID and a *higher* VersionEx, used to instantly defang a deployed policy when `citool --remove-policy` can't be used; **P1/P2/P2a/Pn** = the four policy iterations built in this spike (defined in §3).

## Locked decisions (this session's Q&A)

| # | Decision |
|---|---|
| 1 | VM is **clean** (no DLP agent). The spike copies the repo's `python-embed\` to the VM for the pywin32 event test. |
| 2 | User takes a VMware snapshot **`pre-AC1`** before the first system-modifying step. Snapshot-revert is the last rung of the rollback ladder. |
| 3 | Files reach the VM via **VMware shared folder**; runbook copies everything to VM-local disk first (so a policy that breaks shared-folder access can't strand the run). |
| 4 | SYSTEM context via **PsExec** (`-s -i`, download allowed). `schtasks /ru SYSTEM` is the fallback if PsExec itself gets blocked. |
| 5 | Test matrix: **ALLOW 7-Zip + WinRAR** (from *copied* exes — decision 8a), **DENY OneDrive + new Outlook (`olk.exe`)**. Both deny targets are Microsoft/Store-signed → deliberately tests that explicit Deny rules beat the base policy's Microsoft-signer allows. 7-Zip installer vs installed `7zFM.exe` is the 8b mismatch demo. |
| 6 | Python-under-enforcement: **plan A** (allow-list the python-embed files the listener needs, run live under policy — early self-protect feasibility data) with **plan B fallback** (after policy removal, re-run listener with `EvtSubscribeStartAtOldestRecord` to replay the blocks from the log). |
| 7 | **Pre-allow VMware Tools** user-mode exes in the test policies (Phase 0 dumps their version info first). |
| 8 | Artifacts land in **`interceptors\app_control\spike-results\`** (committed — AC-2/AC-3 are written against them). Keeper scripts in **`scripts\`**. |

## Verified facts (dev-machine probes + web, this session)

- **`citool` on build 26200** (dev = VM build): `--json` global flag exists and *suppresses the interactive Enter-prompt*; bare `citool -h` **hangs waiting on stdin** (why the old tool pipes `echo .`). Unelevated `--list-policies --json` → exit code 5, body `{"OperationResult":-2147024891}` (E_ACCESSDENIED) → **every citool recipe must run elevated/SYSTEM; errors come back as JSON `OperationResult` HRESULTs.** Aliases: `-up/-rp/-lp/-r`.
- **pywin32 `EvtSubscribe` exact signature** ([pywin32 docs](https://timgolden.me.uk/pywin32-docs/win32evtlog__EvtSubscribe_meth.html)): `EvtSubscribe(ChannelPath, Flags, SignalEvent=None, Callback=None, Context=None, Query=None, Session=None, Bookmark=None)` — note `Query` (XPath filter) comes **before** `Session`. Callback receives `(action, context, event_handle)`; `action` is `EvtSubscribeActionDeliver` or `EvtSubscribeActionError`. `EvtRender(handle, EvtRenderEventXml)` yields the XML. All constants confirmed present in the repo's `python-embed` by `dir(win32evtlog)`.
- **Event ID → channel mapping** ([MS Learn event IDs](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/event-id-explanations)) — **corrects parent-plan decision 6**: `Microsoft-Windows-CodeIntegrity/Operational` carries **3077** (enforce block) / **3076** (audit block) / 3033 / 3089 (per-signature info, correlated by ActivityID). **8028/8029 are script/MSI events in a different channel** — `Microsoft-Windows-AppLocker/MSI and Script` — and **packaged apps (MSIX, e.g. olk.exe) block as 8040 (enforce) / 8039 (audit) in that AppLocker channel**, possibly instead of 3077. The spike listener therefore subscribes to **both channels**; which event the olk.exe deny actually produces is itself a spike question (matrix row 14).
- **Deny precedence**: App Control processes all explicit deny rules before allow rules — deny wins ([MS Learn rule precedence](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/design/select-types-of-rules-to-create)).
- **Neutralizer template ships with Windows**: `%windir%\schemas\CodeIntegrity\ExamplePolicies\AllowAll.xml` exists (verified on dev, same build as VM) and already contains `Enabled:Unsigned System Integrity Policy` + `Enabled:Update Policy No Reboot` + `Enabled:UMCI` — matches the MS-documented disable path and parent-plan decision 4.
- **PE version info captured (dev)**: `7zFM.exe`='7zFM', `7z.exe`='7z', `7zG.exe`='7zg', `WinRAR.exe`='WinRAR', `olk.exe`='olk' (readable under WindowsApps without elevation). python-embed: `python.exe`='Python Console'; `python313.dll`, `python3.dll`, **all stdlib `.pyd`s** share `InternalName='Python DLL'` (one allow rule covers them all); pywin32 binaries have per-file names (`win32evtlog.pyd`, `pywintypes313.dll`); `vcruntime140.dll` ok. **Third-party wheel pyds (PyYAML `_yaml…pyd`) have EMPTY version info** → allow lists must be **explicit file lists, never folder scans** (`Add-WDACRule.ps1:111` throws on empty attribute; `add-wdacwrule.py`'s rglob also collects non-PE files).
- `base.xml` has `Disabled:Script Enforcement` → our PowerShell scripts keep working under the deployed policy.
- Already web-verified in the parent session (citations in `app-control-integration-plan.md` §Web-verified): DISM ConfigCI enable on Home, `citool --remove-policy` unsigned-no-reboot on ≥24H2. The spike's job is to **confirm both empirically on this exact VM**.

## Deliverable 1 — repo additions (built on dev before VM day)

New files; follow `scripts\` conventions (`$ErrorActionPreference='Stop'`, `[CmdletBinding()] param(...)`, header comment). PS scripts must be **Windows PowerShell 5.1-compatible** (that's all the VM has).

### 1. `scripts/spike-evt-subscribe.py` (keeper — becomes the AC-3 event-forwarder seed)
Imports: stdlib + `win32evtlog` only (keeps the under-enforcement allow list minimal).
- Args: `--channels` (default BOTH: `Microsoft-Windows-CodeIntegrity/Operational` and `Microsoft-Windows-AppLocker/MSI and Script`), `--events` (default `3033,3076,3077,3089` for CI channel, `8028,8029,8039,8040` for AppLocker channel → per-channel XPath `*[System[(EventID=3077 or ...)]]`), `--out` dir (default `.\events`), `--replay` (use `EvtSubscribeStartAtOldestRecord` instead of `EvtSubscribeToFutureEvents` — the plan-B path), `--duration` seconds (default: until Ctrl+C).
- One `EvtSubscribe(channel, flags, None, Callback=on_event, Context=channel_name, Query=xpath)` per channel; handles kept alive for process lifetime; callback wrapped in try/except (exceptions must never propagate into pywin32) and checks `action == EvtSubscribeActionDeliver`.
- Callback: `xml = win32evtlog.EvtRender(h, win32evtlog.EvtRenderEventXml)`; print + write `{out}\{utc}_{eventid}_{recordid}.xml` (EventID/RecordID parsed with stdlib `xml.etree`).
- Startup banner: user (`getpass.getuser()` — must print `SYSTEM`), pid, channels, flags, out dir.

### 2. `scripts/spike-versioninfo-dump.ps1` (keeper)
`param([string[]]$Paths, [string]$OutCsv)`; folders expand **non-recursively** to `*.exe,*.dll,*.pyd`. Per file: `[System.Diagnostics.FileVersionInfo]` fields (InternalName, OriginalFilename, FileDescription, ProductName, FileVersion, CompanyName) **plus** `Get-AuthenticodeSignature` SignerSubject/Status (answers the OneDrive/VMware/PsExec signing questions on the VM). Warn loudly on empty InternalName (those files would abort the rule builder). Console table + CSV.

### 3. `scripts/spike-neutralize-policy.ps1` (keeper — rehearses the AC-3/AC-5 emergency path)
`param([string]$TargetPolicyXml, [string]$OutputPolicy, [string]$VersionEx, [ValidateSet('AllowAll','AuditFlip')] [string]$Mode = 'AllowAll', [switch]$CompileBinary)`
- **AllowAll** (the neutralizer proper): load `%windir%\schemas\CodeIntegrity\ExamplePolicies\AllowAll.xml`, overwrite its PolicyID/BasePolicyID with the values read from `$TargetPolicyXml` (ours: `{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}`), set `<VersionEx>` to `$VersionEx`, save; optional `ConvertFrom-CIPolicy` → `{PolicyID}.cip`.
- **AuditFlip** (used once, to capture audit-mode 3076/8039 samples): load `$TargetPolicyXml`, insert `<Option>Enabled:Audit Mode</Option>` into `<Rules>` (idempotent), set `$VersionEx`, save/compile.
- Both honor the sipolicy namespace `urn:schemas-microsoft-com:sipolicy`.

### 4. `scripts/spike-ac1-stage.ps1` (keeper — host-side staging)
`param([string]$StageDir = '<repo>\tmp\ac1-stage')` (tmp\ is gitignored). Copies: `python-embed\` (whole), `interceptors\app_control\cli\{base.xml, Add-WDACRule.ps1, add-wdacwrule.py}`, the three spike scripts above, `lists\` templates (below), empty `artifacts\{events,citool-json,versioninfo,policies}` skeleton. Prints "point the VMware shared folder here".

### 5. List templates (staged under `lists\`; **paths unquoted** — `add-wdacwrule.py` does `Path(line)` so quotes break it; the existing `deny-list.txt`'s quoted line is a latent bug, noted for AC-4)
- `p1-allow.txt`: 7-Zip **copies** (`C:\spike\copies\{7zFM.exe,7z.exe,7zG.exe,7-zip.dll,7z.dll}`), WinRAR **copies** (`WinRAR.exe,Rar.exe,RarExt.dll` + whatever the Phase-0 dump shows), the two installers in `C:\Users\agent1\Downloads\`, python-embed explicit files (`python.exe, python313.dll, python3.dll, vcruntime140.dll, vcruntime140_1.dll, Lib\site-packages\pywin32_system32\pywintypes313.dll, Lib\site-packages\win32\{win32evtlog,win32event,win32api}.pyd` — under `C:\spike\python-embed\`), `C:\spike\tools\PsExec64.exe` + the captured `PSEXESVC-copy.exe` (runbook 0.8), VMware Tools exes (appended after the Phase-0 dump).
- `p2-allow.txt` = p1 **minus the five 7-Zip copy lines** (creates the 8b condition: installer still allowed, installed app no longer allowed).
- `p2-deny.txt`: OneDrive.exe + olk.exe — exact paths filled in from runbook 0.6/0.7.

### 6. `interceptors/app_control/spike-results/` (committed)
`RESULTS.md` pre-populated with the §5 matrix (columns: `# | Phase | Command (exact) | Expected | Exit code | Works? | Artifact | Notes`); subfolders `events\`, `citool-json\`, `versioninfo\`, `policies\` filled from the VM run.

## §3 Policy iterations

All share PolicyID `{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}`; every compiled file is named `{PolicyID}.cip`, so **each build gets its own output subfolder**. Builds run on the VM (that's spike scope (a): on-Home compile).

| Policy | VersionEx | Build | Purpose |
|---|---|---|---|
| **P1** "lifeline" | 10.3.0.1 | `python add-wdacwrule.py -i base.xml -o policies\p1\p1.xml -a ..\lists\p1-allow.txt -c` | Deploy recipe (b); VM stays usable; python-under-enforcement (plan A); 8a positive; removal rehearsal (c) on a benign policy **before** the deny policy ever deploys |
| **P2** "deny" | 10.3.0.1 | same + `-d ..\lists\p2-deny.txt`, `-a ..\lists\p2-allow.txt`, `-o policies\p2\p2.xml` | deny-wins test (d), 8b mismatch, live 3077/8040 capture |
| **P2a** "audit" | 10.3.0.5 | `spike-neutralize-policy.ps1 -Mode AuditFlip -TargetPolicyXml policies\p2\p2.xml -OutputPolicy policies\p2a\p2a.xml -VersionEx 10.3.0.5 -CompileBinary` | capture audit-block samples (3076 / 8039) for AC-3's audit filter |
| **Pn** "neutralizer" | 10.3.0.9 | `spike-neutralize-policy.ps1 -Mode AllowAll -TargetPolicyXml base.xml -OutputPolicy policies\pn\pn.xml -VersionEx 10.3.0.9 -CompileBinary` | rehearse the emergency-disable fallback end-to-end |

P1→P2 have **equal VersionEx** → never rely on in-place refresh between them: **remove P1, then deploy P2** (and record what an equal-version refresh actually does as a bonus matrix row). P2a/Pn out-version anything deployed, so they update in place via `citool --refresh` (this is exactly the neutralizer mechanism being rehearsed).

## §4 VM runbook (user-executed; [N]=normal, [E]=elevated PowerShell, [S]=SYSTEM via PsExec)

**Phase 0 — stage + recon (no system changes)**
0.1 [N] Copy shared-folder tree → `C:\spike\` (`cli\, scripts\, lists\, python-embed\, tools\, artifacts\`).
0.2 [N] `Start-Transcript C:\spike\artifacts\transcript.txt`
0.3 [N] `cmd /c ver` → record build 26200 + Home edition.
0.4 [N] `citool --list-policies --json` → expect exit 5 + `{"OperationResult":-2147024891}` (matrix row: unelevated behavior).
0.5 [E] `citool --list-policies --json > C:\spike\artifacts\citool-json\baseline.json` → capture the **field names/casing** of the JSON (AC-3 parser contract).
0.6 [N] Version-info dump → `targets.csv`: `"$env:LOCALAPPDATA\Microsoft\OneDrive\OneDrive.exe"`, `"C:\Program Files\Microsoft OneDrive\OneDrive.exe"`, `C:\Program Files\7-Zip`, `C:\Program Files\WinRAR`, `C:\Program Files\VMware\VMware Tools`, `C:\spike\tools\PsExec64.exe`, the two installers. Fix OneDrive path + append VMware exes into the list templates.
0.7 [N] `Get-AppxPackage *OutlookForWindows* | fl Name,InstallLocation` → pin olk.exe path → fix `p2-deny.txt`. Install 7-Zip + WinRAR from `Downloads\` if absent. Download PsExec to `C:\spike\tools\`.
0.8 [E] Pre-capture PSEXESVC (PsExec's service exe, extracted to `%windir%\PSEXESVC.exe` only while running): `PsExec64.exe -accepteula -s cmd /c "copy /y %windir%\PSEXESVC.exe C:\spike\tools\PSEXESVC-copy.exe"` → dump its version info → append to both allow lists (FileAttrib rules travel with copies, same property as 8a).
0.9 [N] Copy installed 7-Zip/WinRAR files → `C:\spike\copies\` (the 8a fixtures).

**Phase 1 — ConfigCI enable (scope a)** — **GATE: take VMware snapshot `pre-AC1` first.**
1.1 [E] `Get-ChildItem $Env:SystemRoot\servicing\Packages\*ConfigCI*.mum | % { dism /online /norestart /add-package:"$($_.FullName)" }` → record per-package exit codes (0 ok; 3010 = wants reboot — record whether things work anyway).
1.2 [N] In **`powershell`** (5.1): `Get-Command ConvertFrom-CIPolicy` → module present, no reboot.
1.3 [N] Build **P1** (§3) → `.cip` produced = `ConvertFrom-CIPolicy` works on Home (deliverable a). Then build P2, P2a, Pn; eyeball `p2.xml` for `<Deny>` rules + FileRuleRefs in SigningScenario 12.

**Phase 2 — removal rehearsal on benign P1 (scopes b + c-removal)**
2.1 [E] `Copy-Item C:\spike\cli\policies\p1\{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}.cip C:\Windows\System32\CodeIntegrity\CIPolicies\Active\` then `citool --refresh --json` → record.
2.2 [E] `citool --list-policies --json > p1-active.json` → our PolicyID present; capture `IsEnforced/Version/FriendlyName`-style fields exactly.
2.3 [N] Sanity under enforcement: installed `7zFM.exe` runs (**8a positive** — copied-exe rule governs the original, matrix row 12); `C:\spike\python-embed\python.exe -c "import win32evtlog; print('ok')"` (plan-A feasibility); shared folder / network / VMware Tools state (row 9).
2.4 [E] `citool --remove-policy "{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}"` → exit code; `--list-policies --json > after-remove.json` → gone, **no reboot** (scope c primary path). If GUID-format errors: retry without quotes/braces and record which form works.

**Phase 3 — P2 deploy + SYSTEM listener + behavior tests (scopes d, e)**
3.1 [E] Deploy P2 (copy + refresh) → `p2-active.json`.
3.2 [S] `C:\spike\tools\PsExec64.exe -accepteula -s -i C:\spike\python-embed\python.exe C:\spike\scripts\spike-evt-subscribe.py --out C:\spike\artifacts\events` → banner must say `SYSTEM`; leave running. (If PsExec/python blocked → record, fall back to plan B in 4.4.)
3.3 [N] Behavior tests (each = matrix row + captured event):
  - `olk.exe` (Store-signed, base allows Store) → expect **BLOCKED** = deny-beats-allow proof; record whether it surfaces as 3077 (CI channel) or 8040 (AppLocker/MSIX channel).
  - `OneDrive.exe` → expect BLOCKED; **also note**: if it turns out base.xml would have blocked it anyway (its signing chain may lack the EKUs base allows — unproven), the clean precedence proof rests on olk.exe; record both.
  - Installed `7zFM.exe` → **BLOCKED under P2** (its copy-rules were removed) while `Downloads\7z2601-x64.exe` installer **runs** → the **8b installer-vs-installed mismatch**, both events captured.
  - WinRAR still runs (allow intact, control).
3.4 [N] Confirm `events\*.xml` written; the 3077 sample is **the** AC-3 deliverable — note exact element names (`PolicyGUID` vs `PolicyID`, `PolicyName`, FileName fields, SI Signing Scenario).

**Phase 4 — audit capture + neutralizer rehearsal (scope c-fallback)**
4.1 [E] Deploy **P2a** (10.3.0.5) via copy + `citool --refresh` over the live P2 → in-place update works (same PolicyID, higher version, no reboot).
4.2 [N] Re-launch olk/OneDrive/7zFM → they now **run** (audit mode) and produce **3076 / 8039** samples → captured by the still-running listener.
4.3 [E] Deploy **Pn** (AllowAll, 10.3.0.9) via copy + refresh → everything runs, no new block events (neutralizer = immediate relief, rehearsed end-to-end). Then delete the `.cip` from `Active\` → reboot → `citool --list-policies --json`: policy **gone after boot** (the delete-then-reboot half of decision 4).
4.4 [N] **Plan B (only if 3.2 failed):** with no policy active, `…python.exe spike-evt-subscribe.py --replay --out …` → harvest the historical 3077/8040s via `EvtSubscribeStartAtOldestRecord`; record that plan B was needed (that finding changes AC-2's self-protect scope).
4.5 [E] Final `citool --remove-policy` (if anything left) → `--list-policies --json` matches `baseline.json`.

**Phase 5 — wrap**
5.1 [N] `Stop-Transcript`; copy `C:\spike\artifacts\*` out via shared folder → `interceptors\app_control\spike-results\`. 5.2 User reverts VM to `pre-AC1`.

## §5 Results-matrix rows (RESULTS.md skeleton)

1 build/edition · 2 citool present · 3 unelevated `--list-policies --json` = exit 5 + HRESULT JSON · 4 elevated baseline JSON field names · 5 DISM ConfigCI per-.mum exit codes, reboot? · 6 `ConvertFrom-CIPolicy` works post-DISM no-reboot · 7 P1/P2/P2a/Pn builds (any empty-version-info aborts) · 8 P1 deploy copy+refresh · 9 enforcement sanity (7zFM ✓, python ✓/✗, VMware Tools state) · 10 `--remove-policy` no-reboot + accepted GUID format · 11 P2 deploy · 12 **8a**: copied-exe rules govern originals · 13 **8b**: installer allowed ≠ installed allowed · 14 **deny-wins**: olk.exe blocked (+ which event ID/channel MSIX blocks use) · 15 OneDrive blocked + P1-era control observation · 16 listener as SYSTEM (banner) · 17 **3077 sample XML → exact field names** · 18 3076/8039 audit samples · 19 P2a in-place refresh (same ID, higher version) · 20 neutralizer Pn immediate relief + .cip-delete→gone-after-reboot · 21 plan A vs plan B outcome · 22 equal-VersionEx refresh behavior (bonus).

## Risks & rollback

- **Rollback ladder at every deploy step:** `citool --remove-policy {GUID}` → if that fails, deploy Pn + `citool --refresh` (instant allow-all relief) → if that fails, revert to `pre-AC1` snapshot. Before any revert: copy `artifacts\` out via the shared folder (Phase 5.1 can run early).
- **VMware Tools/UMCI**: pre-allowed (decision 7), but if the Phase-0 dump shows empty InternalNames on some VMware exes, fall back to accepting degraded Tools (everything already staged locally).
- **PsExec blocked under policy**: PsExec64 + captured PSEXESVC are pre-allowed; fallback `schtasks /create /ru SYSTEM` (Task Scheduler is Windows-signed).
- **olk.exe MSIX semantics**: deny may act at app-activation rather than process-exec and log 8040 not 3077 — that's a finding to record, not a failure.
- **Kernel side (KMCI)**: VMware drivers are WHQL-signed and base.xml allows WHQL → expected fine; if networking/shared folder die under P1, record + remove policy (that finding would reshape the channel design).
- **Equal-VersionEx P1→P2**: routed around via remove-then-deploy.

## Verification (AC-1 done when)

1. The four keeper scripts exist in `scripts\` and run clean on dev where testable (`spike-versioninfo-dump.ps1` on dev paths; `spike-evt-subscribe.py --replay` on dev unelevated reads the CI log or errors gracefully; `spike-neutralize-policy.ps1` produces XML that `ConvertFrom-CIPolicy` compiles **on dev** — compile is side-effect-free, no deploy; stage script fills `tmp\ac1-stage`).
2. `pytest scripts/harness` still green (35 passed / 3 skipped) — nothing in this phase should touch it, run as a regression gate.
3. The VM runbook executed: `interceptors\app_control\spike-results\RESULTS.md` has all ~22 rows filled, `events\` holds at least one real 3077 sample (+ 8040 if that's what MSIX produced, + 3076/8039 audit samples), `citool-json\` holds baseline/p1/p2/after-remove captures.
4. Parent plan updated with the two corrections this spike already surfaced (after VM confirmation): decision-6 event mapping (3076=audit twin of 3077; 8028/8029/8039/8040 live in the AppLocker *MSI and Script* channel; AC-3 forwarder must subscribe to both channels) and the unquoted-list-file requirement.

**Dev-machine safety:** no policy is ever deployed on dev — dev work is file creation, read-only `citool` queries, and side-effect-free `ConvertFrom-CIPolicy` compiles into the repo's `tmp\`.
