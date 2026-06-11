# AC-1 Spike — VM Runbook + Results Matrix

Target: Windows 11 Home build 26200 VM (VMware Workstation, user `agent1`, clean — no DLP agent).
Stage the payload with `scripts\spike-ac1-stage.ps1` on the dev machine, point the shared folder at `tmp\ac1-stage\`, copy to `C:\spike\` inside the guest.
Markers: **[N]** normal PowerShell, **[E]** elevated PowerShell, **[S]** SYSTEM via PsExec.
Policies: **P1** lifeline (allows only, 10.3.0.1) / **P2** deny (10.3.0.1) / **P2a** audit flip of P2 (10.3.0.5) / **Pn** AllowAll neutralizer (10.3.0.9). All share PolicyID `{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}`.

**Rollback ladder at every deploy step:** `citool --remove-policy "{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}"` → if that fails: deploy `Pn` .cip + `citool --refresh` (instant allow-all) → if that fails: copy `C:\spike\artifacts\` out via shared folder, revert to snapshot `pre-AC1`.

## Runbook

### Phase 0 — stage + recon (no system changes)
- 0.1 [N] copy shared-folder tree → `C:\spike\`
- 0.2 [N] `Start-Transcript C:\spike\artifacts\transcript.txt`
- 0.3 [N] `cmd /c ver`
- 0.4 [N] `citool --list-policies --json` (expect exit 5 + `{"OperationResult":-2147024891}`)
- 0.5 [E] `citool --list-policies --json | Out-File C:\spike\artifacts\citool-json\baseline.json`
- 0.6 [N] version-info dump (fix OneDrive path / append VMware exes into BOTH allow lists afterwards). In the PowerShell session run `Set-ExecutionPolicy -Scope Process Bypass -Force` once, then invoke the script DIRECTLY (array args do not survive `powershell -File` from another shell):
  `C:\spike\scripts\spike-versioninfo-dump.ps1 -OutCsv C:\spike\artifacts\versioninfo\targets.csv -Paths "$env:LOCALAPPDATA\Microsoft\OneDrive\OneDrive.exe","C:\Program Files\Microsoft OneDrive\OneDrive.exe","C:\Program Files\7-Zip","C:\Program Files\WinRAR","C:\Program Files\VMware\VMware Tools","C:\spike\tools\PsExec64.exe","C:\Users\agent1\Downloads\7z2601-x64.exe","C:\Users\agent1\Downloads\winrar-x64-722.exe"`
- 0.7 [N] `Get-AppxPackage *OutlookForWindows* | fl Name,InstallLocation` → fix `lists\p2-deny.txt`; install 7-Zip + WinRAR if absent; download PsExec64.exe → `C:\spike\tools\`
- 0.8 [E] capture PSEXESVC: `C:\spike\tools\PsExec64.exe -accepteula -s cmd /c "copy /y %windir%\PSEXESVC.exe C:\spike\tools\PSEXESVC-copy.exe"` → dump its version info too
- 0.9 [N] copy installed 7-Zip (`7zFM.exe,7z.exe,7zG.exe,7-zip.dll,7z.dll`) + WinRAR (`WinRAR.exe,Rar.exe,RarExt.dll`) files → `C:\spike\copies\`

### Phase 1 — ConfigCI enable — **GATE: take VMware snapshot `pre-AC1` first**
- 1.1 [E] `Get-ChildItem $Env:SystemRoot\servicing\Packages\*ConfigCI*.mum | % { dism /online /norestart /add-package:"$($_.FullName)" }`
- 1.2 [N] in Windows PowerShell 5.1: `Get-Command ConvertFrom-CIPolicy`
- 1.3 [N] build all four (run from `C:\spike\cli`, using the embed python — the VM has no other python):
  - `C:\spike\python-embed\python.exe add-wdacwrule.py -i base.xml -o policies\p1\p1.xml -a ..\lists\p1-allow.txt -c`
  - `C:\spike\python-embed\python.exe add-wdacwrule.py -i base.xml -o policies\p2\p2.xml -a ..\lists\p2-allow.txt -d ..\lists\p2-deny.txt -c`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File ..\scripts\spike-neutralize-policy.ps1 -TargetPolicyXml policies\p2\p2.xml -OutputPolicy policies\p2a\p2a.xml -VersionEx 10.3.0.5 -Mode AuditFlip -CompileBinary`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File ..\scripts\spike-neutralize-policy.ps1 -TargetPolicyXml base.xml -OutputPolicy policies\pn\pn.xml -VersionEx 10.3.0.9 -Mode AllowAll -CompileBinary`

### Phase 2 — removal rehearsal on benign P1
- 2.1 [E] `Copy-Item 'C:\spike\cli\policies\p1\{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}.cip' C:\Windows\System32\CodeIntegrity\CIPolicies\Active\` then `citool --refresh --json`
- 2.2 [E] `citool --list-policies --json | Out-File C:\spike\artifacts\citool-json\p1-active.json`
- 2.3 [N] sanity: launch installed `7zFM.exe` (expect RUNS — 8a positive); `C:\spike\python-embed\python.exe -c "import win32evtlog; print('ok')"`; check shared folder / network / VMware Tools
- 2.4 [E] `citool --remove-policy "{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}"` then `citool --list-policies --json | Out-File C:\spike\artifacts\citool-json\after-remove.json` (no reboot; if GUID format rejected, retry without quotes/braces and record which works)

### Phase 3 — P2 + SYSTEM listener + behavior tests
- 3.1 [E] deploy P2 .cip (copy + `citool --refresh --json`) → save `p2-active.json`
- 3.2 [S] `C:\spike\tools\PsExec64.exe -accepteula -s -i C:\spike\python-embed\python.exe C:\spike\scripts\spike-evt-subscribe.py --out C:\spike\artifacts\events` (banner must say `user=SYSTEM`; leave running)
- 3.3 [N] launch in turn: `olk.exe` (expect BLOCKED), `OneDrive.exe` (expect BLOCKED), installed `7zFM.exe` (expect BLOCKED), `Downloads\7z2601-x64.exe` (expect RUNS), `WinRAR.exe` (expect RUNS)
- 3.4 [N] confirm `C:\spike\artifacts\events\*.xml` written; note exact field names in a 3077 sample

### Phase 4 — audit capture + neutralizer rehearsal
- 4.1 [E] deploy P2a .cip + `citool --refresh --json` (in-place update, same PolicyID, higher VersionEx)
- 4.2 [N] re-launch olk / OneDrive / 7zFM → now RUN, audit events 3076/8039 captured
- 4.3 [E] deploy Pn .cip + `citool --refresh --json` → everything runs, no block events; then `Remove-Item 'C:\Windows\System32\CodeIntegrity\CIPolicies\Active\{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}.cip'`; reboot; `citool --list-policies --json` → policy gone after boot
- 4.4 [N] **plan B only if 3.2 failed:** `C:\spike\python-embed\python.exe C:\spike\scripts\spike-evt-subscribe.py --replay --duration 30 --out C:\spike\artifacts\events`
- 4.5 [E] final `citool --remove-policy` if anything left; `--list-policies --json` matches baseline

### Phase 5 — wrap
- 5.1 [N] `Stop-Transcript`; copy `C:\spike\artifacts\*` → shared folder → this directory (`events\`, `citool-json\`, `versioninfo\`); copy `C:\spike\cli\policies\*\*.xml` → `policies\`
- 5.2 revert VM to snapshot `pre-AC1`

## Results matrix (fill during the run)

| #   | Phase   | Test                           | Command (exact)                       | Expected                      | Exit code | Works?                                                                           | Artifact          | Notes                                 |
| --- | ------- | ------------------------------ | ------------------------------------- | ----------------------------- | --------- | -------------------------------------------------------------------------------- | ----------------- | ------------------------------------- |
| 1   | 0.3     | build/edition                  | `cmd /c ver`                          | 26200, Home                   |           | Yes                                                                              | transcript        |                                       |
| 2   | 0.4     | citool present                 | `citool --list-policies --json` [N]   | exit 5, HRESULT JSON          |           | Yes                                                                              | transcript        |                                       |
| 3   | 0.5     | list-policies baseline         | same [E]                              | exit 0, JSON schema           |           | Yes                                                                              | baseline.json     | record field names/casing             |
| 4   | 0.6/0.8 | version-info dump              | spike-versioninfo-dump.ps1            | all targets have InternalName |           | Not all have internal names, some VMWare related DLLs do not have Internal names | targets.csv       | OneDrive path? signers?               |
| 5   | 1.1     | DISM ConfigCI on Home          | dism /add-package loop                | exit 0 (or 3010?), no reboot  |           | Yes                                                                              | transcript        | per-.mum codes                        |
| 6   | 1.2     | ConfigCI usable                | `Get-Command ConvertFrom-CIPolicy`    | found, PS 5.1                 |           | Yes                                                                              | transcript        |                                       |
| 7   | 1.3     | P1/P2/P2a/Pn build+compile     | add-wdacwrule.py / neutralize ps1     | 4 .cip files                  |           | Yes                                                                              | policies\         | any empty-version aborts?             |
| 8   | 2.1     | deploy recipe                  | copy + `citool --refresh --json`      | applied, no reboot            |           | Yes                                                                              | p1-active.json    |                                       |
| 9   | 2.3     | enforcement sanity             | run 7zFM / python / Tools check       | all alive                     |           | Yes                                                                              | transcript        | VMware Tools state!                   |
| 10  | 2.4     | remove-policy no-reboot        | `citool --remove-policy {GUID}`       | removed, no reboot            |           | Yes                                                                              | after-remove.json | accepted GUID format?                 |
| 11  | 3.1     | P2 deploy                      | copy + refresh                        | applied                       |           | Yes                                                                              | p2-active.json    |                                       |
| 12  | 2.3     | **8a** copies govern originals | installed 7zFM under P1               | RUNS                          |           | Yes                                                                              | transcript        |                                       |
| 13  | 3.3     | **8b** installer≠installed     | installer runs, 7zFM blocked under P2 | mismatch shown                |           | Yes                                                                              | events\           |                                       |
| 14  | 3.3     | **deny-wins** (Store-signed)   | launch olk.exe                        | BLOCKED                       |           | Yes                                                                              | events\           | 3077 or 8040?                         |
| 15  | 3.3     | OneDrive deny                  | launch OneDrive.exe                   | BLOCKED                       |           | Yes                                                                              | events\           | was it base-blocked under P1 anyway?  |
| 16  | 3.2     | listener as SYSTEM             | PsExec -s -i python …                 | banner user=SYSTEM            |           | Yes                                                                              | transcript        |                                       |
| 17  | 3.4     | **3077 field names**           | inspect sample XML                    | PolicyGUID/PolicyName pinned  |           | Yes                                                                              | events\3077-*.xml | THE AC-3 deliverable                  |
| 18  | 4.2     | audit samples                  | relaunch under P2a                    | 3076 / 8039 captured          |           | Yes                                                                              | events\           |                                       |
| 19  | 4.1     | in-place version bump          | P2a refresh over P2                   | applied, no reboot            |           | Yes                                                                              | citool-json\      |                                       |
| 20  | 4.3     | neutralizer end-to-end         | Pn refresh; delete .cip; reboot       | relief now; gone after boot   |           | Yes                                                                              | citool-json\      |                                       |
| 21  | 3.2/4.4 | plan A vs plan B               | python under enforcement?             | A works                       |           | A works, yes                                                                     | transcript        | if B: changes AC-2 self-protect scope |
| 22  | bonus   | equal-VersionEx refresh        | optional: P2 refresh over P1          | record behavior               |           | P2 seems to refresh over p1                                                      |                   | not relied upon                       |

## Pinned contracts (AC-1 outcomes — what AC-2/AC-3 code is written against)

**Run completed 2026-06-11 on the Win11 Home 26200 VM. All 22 matrix rows pass; plan A (python live under enforcement as SYSTEM) worked.**

### 3077/3076 event payload (sample: `events\20260611T013444_3077_428.xml`, Version=5)
- **Filter on `<Data Name='PolicyGUID'>` = `{9dbaa326-cb59-4b1d-abaf-b28412229e4a}`** (lowercase, WITH braces). **NOT** on `PolicyID` — that Data field carries the policy XML's `Settings\Id` string (our tool stamps the build date, e.g. `2026-06-10`), not the GUID. `PolicyName` = the policy's Name setting (`base`).
- Several field names contain SPACES: `File Name`, `Process Name`, `Requested Signing Level`, `Validated Signing Level`, `SI Signing Scenario`, `SHA1 Hash`, `SHA256 Flat Hash`.
- Payload includes PE metadata (`OriginalFileName`, `InternalName`, `FileDescription`, `ProductName`, `FileVersion`), `UserWriteable`, and `PackageFamilyName` for MSIX apps. Paths are NT device paths (`\Device\HarddiskVolume3\...`).
- 3076 (audit) payload shape is identical to 3077. Each block also emits 3033 + correlated 3089 signature-info events (same `Correlation ActivityID`).
- **MSIX blocks surface as 3077 in CodeIntegrity/Operational** (olk.exe, `Process Name`=svchost.exe via app activation) — NO 8039/8040 appeared in the AppLocker channel during the whole run. AC-3: CI channel is the primary feed; keep the AppLocker subscription as cheap insurance.

### citool --list-policies --json schema (build 26200; `citool-json\*.json`)
- Top level: `{"Policies":[...]}`; error shape: `{"OperationResult":<HRESULT int>}`; success on refresh: `{"OperationResult":0}`.
- Per-policy fields: `PolicyID` / `BasePolicyID` (lowercase GUID, **no braces** — unlike the event's PolicyGUID), `FriendlyName`, `Version` (packed uint64), `VersionString` (`"10.3.0.1"`), `IsSystemPolicy`, `IsSignedPolicy`, `IsOnDisk`, `IsEnforced`, `IsAuthorized`, `PolicyOptions` (string array).
- Round trip evidenced: baseline 14 policies → P1 deployed 15 (`IsEnforced:true`) → after remove 14 → post-reboot (after Pn .cip delete + final remove) 14 = baseline.
- `citool --remove-policy "{GUID}"` (quoted, braced GUID accepted) → `Operation Successful`, **no reboot**. **It prompts on stdin (`Press Enter to Continue/Exit`) BOTH with and without `--json`** — the help text's "--json … suppress input" claim does NOT hold for remove-policy. AC-3 recipe: always redirect stdin on every citool call (pipe a newline like the existing `echo . | citool -r`, or `stdin=subprocess.DEVNULL` so the read hits EOF) **and** pass `--json` for the parseable output.

### Behavior-test evidence (events summarized)
- 8a: installed 7zFM.exe RAN under P1 (rules built from copies govern originals).
- 8b: under P2, `7z2601-x64.exe` installer ran while installed `7zFM.exe` → 3077 (`20260611T013515_3077_440.xml`).
- Deny-wins: olk.exe (Store-allowed by base) → 3077 ×2; OneDrive.exe → 3077.
- Row-15 control answered by the P2a audit data: OneDrive's own DLLs (FileSyncClient.dll, SyncEngine.dll, …) produced 3076s **with no deny rule on them** → OneDrive's signing chain (Microsoft Code Signing PCA, no Windows/Store EKU) fails the base allow set anyway; the clean deny-beats-allow proof is olk.exe.
- Bonus: WinRAR (allowed) spawned `RarExtInstaller.exe` → 3077 — unallowed helper inside an allowed app; real-world allow-list-granularity example for AC-2 docs.
- Neutralizer: P2a (audit, 10.3.0.5) refreshed in place over P2 (`OperationResult:0`), Pn (AllowAll, 10.3.0.9) over P2a, `.cip` delete + reboot → policy gone.

### SYSTEM-context caveat
`getpass.getuser()` under LocalSystem returns the **machine account** (`VM-WIN$`), not `SYSTEM` — AC-3 health logging should not string-match "SYSTEM".

### Not preserved
The built policy XMLs (p1/p2/p2a/pn) were lost with the snapshot revert; the as-built input lists are kept in `policies\` and the build is reproducible from `cli\base.xml` + those lists.

## Findings to feed back into the parent plan after the run

- Decision-6 correction (already web-verified, confirm empirically): audit twin of 3077 is **3076**; 8028/8029 + packaged-app 8039/**8040** live in `Microsoft-Windows-AppLocker/MSI and Script` — AC-3 forwarder must subscribe to **both** channels.
- List files must contain **unquoted** paths (existing `cli\deny-list.txt` line is quoted — latent bug for AC-4).
- **Command-line length limit (hit at step 1.3):** `add-wdacwrule.py` passes ALL paths as one comma-joined argument to `powershell -File` — a folder in a list explodes to thousands of paths and `CreateProcess` fails with `WinError 206` (~32K limit) before PowerShell even runs. AC-4's builder must not shell out path lists on the command line (the AC-2 Python port avoids this inherently); also: deny/allow list entries must be FILES, never folders.
- **Generic InternalName hazard:** OneDrive.exe's InternalName is `Client Application` — a deny rule on it blocks *anything* sharing that InternalName. AC-2's builder should warn on overly generic attribute values (same class of warning as the installer-like-name check, decision 8b).
- **Hash-fallback case confirmed in the wild:** VMware Tools ships `pcre2-8.dll` with NO version info at all (and `glibmm-2.68-1.dll`/`sigc-3.0-0.dll` with OriginalFilename only) — FileAttrib rules cannot cover such files; decision 5's hash fallback is required for real-world coverage.
- ConfigCI DISM enable on Home 26200: all 12 `*ConfigCI*.mum` packages added with "operation completed successfully", **no reboot** (matrix row 5 confirmed).
- `Set-ExecutionPolicy -Scope Process Bypass -Force` is needed **per shell**, including each new elevated shell (0.6 first attempt failed on this in the [E] shell).
- Whatever rows 9/14/15/21 reveal about VMware Tools, MSIX denies, and python-under-policy.
