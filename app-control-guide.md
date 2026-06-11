# App Control channel — understand & test guide

This guide covers the **app-control channel** that lives at `interceptors/app_control/cli/`. It is currently a stand-alone tool — it does not talk to the agent core, the orchestrator, or any pipe. It builds and (optionally) deploys a **WDAC** policy that decides which user-mode executables are allowed to start on the host.

The guide is split in two:

1. **Quick test** — minimal commands you can run *right now*, mostly on the dev box, to confirm the tool works.
2. **Full picture** — what every file does, what each flag means at the OS level, and how to test enforcement safely in the VM.

> **Terminology key** (forward-references used throughout):
> - **WDAC** — Windows Defender Application Control. The Windows kernel feature that decides at process load time whether a user-mode binary is allowed to run. Microsoft's newer brand is *App Control for Business*; both names refer to the same thing.
> - **UMCI** — User-Mode Code Integrity. The WDAC sub-system that polices user-mode binaries (the `<SigningScenario Value="12">` block in `base.xml`).
> - **SiPolicy XML** — the XML schema WDAC policies are authored in (`urn:schemas-microsoft-com:sipolicy`). Root element is `<SiPolicy>`.
> - **.cip** — the compiled binary form of a SiPolicy XML, produced by `ConvertFrom-CIPolicy`. The OS only loads `.cip` files, not raw XML.
> - **CIPolicies\\Active** — `C:\Windows\System32\CodeIntegrity\CIPolicies\Active\`. The folder the OS scans for `.cip` files when refreshing policy. Filename must be `{PolicyID}.cip`.
> - **CiTool** — `citool.exe`, shipped in Windows 11 22H2+. Manages loaded WDAC policies. `citool -r` (alias of `--refresh`) tells the kernel to re-read `CIPolicies\Active`.
> - **PE version-info** — the fixed metadata block in a Windows `.exe` / `.dll` (Internal name, Original filename, Product name, etc.). The tool reads this to identify binaries by attribute rather than by signature.

---

## 1. Quick test (validate it runs)

The tool has three observable layers, in increasing order of consequence. **Stop at the highest layer you have already verified before moving on.**

| # | What it proves | Side effects | Where to run |
|---|---|---|---|
| A | Python wrapper works, paths parse | None — `-h` only | Dev box |
| B | XML rule insertion works | Writes one XML file to `%TEMP%` | Dev box |
| C | ConfigCI compiler is installed and the XML is valid | Adds one `.cip` file in `%TEMP%` | Dev box |
| D | Policy actually blocks/allows binaries | **WDAC enforcement activates on the host** | **VM only** |

### Setup (do this first)

Two hosts, two different prep steps.

**Dev box** — usually nothing to install. The repo uses a Python venv at `.venv\`; activate it and `python` resolves to the venv's interpreter, which is what every command below assumes. If you're outside the venv, the system-wide install is at `C:\Python314\python.exe` — that's the absolute-path fallback used throughout this guide. The WDAC `ConfigCI` PowerShell module ships by default on Windows 11 Pro/Enterprise (the edition the dev box runs); confirm it's there with:

```powershell
Get-Command ConvertFrom-CIPolicy
```

**VM (Windows 11 Home)** — `ConfigCI` is *not* installed by default on Home, and there is no `Add-WindowsCapability` path for it (Home doesn't expose any `Rsat.CodeIntegrity` capability). The fix is to activate the staged component packages directly with DISM. This is documented to work on Windows 11 23H2 and later (build 26200 / 25H2 is fine):

1. **Snapshot the VM** in VMware Workstation (*VM → Snapshot → Take Snapshot…*); label it `pre-ConfigCI-install`. The DISM step modifies component-store state and snapshot revert is the cleanest reversal.

2. **Confirm the staged `.mum` files exist** (read-only, no risk):

   ```powershell
   Get-ChildItem $Env:SystemRoot\servicing\Packages\*ConfigCI*.mum | Select-Object Name
   ```

   You should see at least one `.mum`. If the list is empty, this route can't work on your image — fall back to compiling `.cip` on the dev box and copying it to the VM.

3. **Install** in an **elevated** PowerShell:

   ```powershell
   gci $Env:SystemRoot\servicing\Packages\*ConfigCI*.mum | % { dism /online /norestart /add-package:"$_" }
   ```

   No reboot required; the components are user-mode PowerShell module files. Each `.mum` should end with `The operation completed successfully.`

4. **Verify**:

   ```powershell
   Get-Module -ListAvailable ConfigCI | Select-Object Name, Version
   Get-Command ConvertFrom-CIPolicy
   Test-Path 'C:\Windows\System32\WindowsPowerShell\v1.0\Modules\ConfigCI'
   ```

   Expect a row for `ConfigCI`, a non-empty `Get-Command` result, and `True`.

The VM does not use a venv — `python` resolves to the system install directly, so the absolute-path fallback is rarely needed there. Substitute your VM's Python path if `python` ever fails to resolve.

> Why the `.mum` trick works: `.mum` (Microsoft Update Manifest) files describe component packages already staged under `C:\Windows\servicing\Packages\`. Home doesn't activate the ConfigCI package by default, but its payload is on disk; `dism /add-package` activates it. Per a community report ([valinet/ssde#9](https://github.com/valinet/ssde/issues/9)), this is the known route on Home from 23H2 onward. It's not Microsoft-documented for Home — snapshot first.

### A. Help

```powershell
python interceptors\app_control\cli\add-wdacwrule.py -h
```

Absolute-path fallback (use when `python` is not on PATH — typical when you're outside the venv on the dev box):

```powershell
C:\Python314\python.exe interceptors\app_control\cli\add-wdacwrule.py -h
```

Expected: argparse usage block with `-i`, `-o`, `--allow-paths`, `--deny-paths`, `-c`, `--deploy`. **Tested on dev box: works.**

### B. XML generation (no compile, no deploy)

Pick any two binaries on the dev box you know exist (Notepad / Calc are reliable):

```powershell
mkdir $env:TEMP\wdac-smoke -Force | Out-Null
python interceptors\app_control\cli\add-wdacwrule.py `
  -i interceptors\app_control\cli\base.xml `
  -o $env:TEMP\wdac-smoke\out.xml `
  --allow-paths C:\Windows\System32\notepad.exe `
  --deny-paths  C:\Windows\System32\calc.exe
```

Absolute-path fallback for the `python` invocation (everything else is plain PowerShell and needs no fallback):

```powershell
C:\Python314\python.exe interceptors\app_control\cli\add-wdacwrule.py `
  -i interceptors\app_control\cli\base.xml `
  -o $env:TEMP\wdac-smoke\out.xml `
  --allow-paths C:\Windows\System32\notepad.exe `
  --deny-paths  C:\Windows\System32\calc.exe
```

Expected output (last 4 lines roughly):

```
Output : C:\Users\...\Temp\wdac-smoke\out.xml
Version: 10.3.0.1   PolicyInfo.Id: <today>
Added  : 1 <Allow> rule(s) + 1 <Deny> rule(s) + 2 <FileRuleRef> entry/entries
```

Verify the inserted rules:

```powershell
Select-String -Path $env:TEMP\wdac-smoke\out.xml -Pattern "ID_ALLOW_A_1|ID_DENY_D_1|VersionEx"
```

You should see one `<Allow>` and one `<Deny>` element with `InternalName="..."` attributes, two `<FileRuleRef>` entries, and `VersionEx` bumped from `10.3.0.0` to `10.3.0.1`. **Tested on dev box: works.**

### C. Compile to `.cip` (still no deploy)

Add `-c` to the previous command. This invokes `ConvertFrom-CIPolicy` from the `ConfigCI` PowerShell module.

```powershell
python interceptors\app_control\cli\add-wdacwrule.py `
  -i interceptors\app_control\cli\base.xml `
  -o $env:TEMP\wdac-smoke\out.xml `
  --allow-paths C:\Windows\System32\notepad.exe `
  -c
```

Absolute-path fallback:

```powershell
C:\Python314\python.exe interceptors\app_control\cli\add-wdacwrule.py `
  -i interceptors\app_control\cli\base.xml `
  -o $env:TEMP\wdac-smoke\out.xml `
  --allow-paths C:\Windows\System32\notepad.exe `
  -c
```

Expected: a file named `{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}.cip` (the `PolicyID` from `base.xml`) appears next to `out.xml`. Confirm:

```powershell
Get-ChildItem $env:TEMP\wdac-smoke
```

Prereq check (run once if you suspect it's missing):

```powershell
Get-Command ConvertFrom-CIPolicy   # Should show ModuleName = ConfigCI
```

**Tested on dev box: works. `ConfigCI` and `citool.exe` are both present.**

### D. Enforce on a live host — VM only

This is the only step where the OS actually starts blocking processes. Run **inside the VM, in an elevated PowerShell**.

#### Command shape (formula with placeholders)

```powershell
python interceptors\app_control\cli\add-wdacwrule.py `
  -i <BASE_XML> `                                # input WDAC policy to extend; use base.xml
  -o <OUT_XML> `                                 # output: modified XML written here (parent dir must exist)
  [--allow-paths <BIN> [<BIN>...]] `             # inline allow: file(s) or folder(s); folders are walked recursively
  [--deny-paths  <BIN> [<BIN>...]] `             # inline deny: file(s) or folder(s)
  [--allow-list  <TXT>] `                        # OR list file: one path per line, '#' for comments
  [--deny-list   <TXT>] `                        # OR list file: same format as --allow-list
  [-f {InternalName|OriginalFileName|FileDescription|ProductName}] `  # PE version-info field used to identify binaries (default: InternalName)
  --deploy                                       # implies -c (compile): write .cip, copy to CIPolicies\Active, run 'citool -r'
```

At least one of `--allow-paths` / `--deny-paths` / `--allow-list` / `--deny-list` must be supplied. `--deploy` is the dangerous bit — it makes the policy go live; until you pass `--deploy` (or `-c` plus a manual copy/refresh) nothing on disk outside `<OUT_XML>` changes.

`base.xml` enables **`Update Policy No Reboot`**, so no reboot is needed to apply the policy and none is needed to remove it (see *Rolling it back* below).

#### Real example

The repo's `allow-list.txt` and `deny-list.txt` reference paths under `C:\Users\agent1\...` — those are the original author's binaries, not yours. **Do not** use those lists as-is on the VM (the wrapper will exit with `error: Path not found:` before doing anything). Point the flags at real binaries on your VM, or write fresh list files.

This command was confirmed working on the VM (denies the bundled new Outlook):

```powershell
New-Item -ItemType Directory -Path C:\tmp -Force | Out-Null

python interceptors\app_control\cli\add-wdacwrule.py `
  -i interceptors\app_control\cli\base.xml `
  -o C:\tmp\out.xml `
  --deny-paths "C:\Program Files\WindowsApps\Microsoft.OutlookForWindows_<version>_x64__8wekyb3d8bbwe\olk.exe" `
  --deploy
```

Absolute-path fallback for the `python` invocation:

```powershell
C:\Python314\python.exe interceptors\app_control\cli\add-wdacwrule.py `
  -i interceptors\app_control\cli\base.xml `
  -o C:\tmp\out.xml `
  --deny-paths "C:\Program Files\WindowsApps\Microsoft.OutlookForWindows_<version>_x64__8wekyb3d8bbwe\olk.exe" `
  --deploy
```

Replace `<version>` with whatever build of the new Outlook is installed on your VM. If you're not sure, wildcard it first:

```powershell
(Get-ChildItem "C:\Program Files\WindowsApps\Microsoft.OutlookForWindows_*").FullName
```

Expected tail of output:

```
Deploying: ...\C:\tmp\{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}.cip
     ->    C:\Windows\System32\CodeIntegrity\CIPolicies\Active\{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}.cip
Refreshing CI policy: echo . | citool -r
Deployed and refreshed.
```

#### Acceptance — what to look for after deploy

The explicit Deny target you passed should be blocked; everything Microsoft-signed (Edge, Notepad, PowerShell, Settings, etc.) still launches, because the signer rules in `base.xml` cover the entire Microsoft trust chain. To also see the *implicit-deny* behavior — i.e. that any non-Microsoft unsigned `.exe` is blocked unless explicitly allowed — do this:

1. Download any small non-Microsoft-signed binary to the VM (a portable Notepad++ or the 7-Zip installer is fine). **Do not run it yet.**
2. Confirm it's not Microsoft-signed:
   ```powershell
   Get-AuthenticodeSignature C:\path\to\thatfile.exe | Select-Object Status, SignerCertificate
   ```
   `SignerCertificate.Subject` should not contain *"Microsoft"*. On a fully unsigned binary, `Status` will be `NotSigned`.
3. Try to launch it. Expected: a brief dialog *"This app has been blocked by your system administrator"* (or the launch silently fails with no UI in some cases — the event log is the authoritative signal).
4. Open **Event Viewer → Applications and Services Logs → Microsoft → Windows → CodeIntegrity → Operational**. Look for **EventID 3077** (block under enforcement) referencing your binary's path. If you're in audit mode (see *Safer testing* below), look for **EventID 8028** instead — same target, no actual block.

The denied Outlook target produces the same `3077` event when launched from Start.

**Not tested by me.** I cannot run the VM from this session, and I will not attempt step D on the dev box: it would block Visual Studio, Python, and most non-Microsoft tools, and require recovery to undo. The Outlook-deny case above is what *you* confirmed worked on the VM.

#### Rolling it back (VM)

`base.xml` is unsigned and has `Enabled:Unsigned System Integrity Policy` + `Enabled:Update Policy No Reboot`, so removal is two steps in elevated PowerShell — no reboot, no Safe Mode:

```powershell
citool --remove-policy "{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}"
# or, equivalently:
Remove-Item "C:\Windows\System32\CodeIntegrity\CIPolicies\Active\{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}.cip" -Force
citool -r
```

Take a VM snapshot before step D anyway — it's free insurance against a misedited base policy.

#### Safer testing — flip to audit mode first

If you want to see *what would* be blocked without actually blocking anything, edit `base.xml` and add this line inside `<Rules>`:

```xml
<Rule><Option>Enabled:Audit Mode</Option></Rule>
```

In audit mode the kernel logs an `8028` event for each binary that *would have been blocked* but lets it run. That's the right first test pass before enforcement.

---

## 2. How it works (full detail)

### 2.1 The four files in `interceptors/app_control/cli/`

| File | Role |
|---|---|
| `base.xml` | Starting WDAC policy. Trusts only Microsoft signers; everything else is implicitly denied. Rule attributes set: `UMCI` enabled, `Unsigned System Integrity Policy` allowed, `Update Policy No Reboot` enabled. **Audit Mode is NOT enabled — this base goes straight to enforcement on deploy.** |
| `Add-WDACRule.ps1` | Does the actual XML surgery (insert `<Allow>` / `<Deny>` rules, bump version, optionally compile). |
| `add-wdacwrule.py` | Thin wrapper around the PS1: parses CLI args, expands directories→files, optionally deploys the compiled `.cip`. |
| `allow-list.txt` / `deny-list.txt` | Example input lists. Plain text, one path per line, `#` for comments, BOM-tolerant. |
| `run.txt` | The author's example command line for reference. |

### 2.2 The pipeline, end to end

```
allow/deny paths ──► add-wdacwrule.py ──► Add-WDACRule.ps1 ──► out.xml
                                                    │
                                                    └─► ConvertFrom-CIPolicy ──► {PolicyID}.cip
                                                                                   │
                                              copy to CIPolicies\Active ◄──────────┘
                                                    │
                                                    └─► citool -r ──► kernel re-reads policy
```

Cause→effect at each arrow:

1. **`add-wdacwrule.py` collects file paths.** Directories get walked recursively (`Path.rglob`). List files (`-a` / `-d`) are read with `#` comments and BOM stripped. Missing paths → `FileNotFoundError`, the script exits with code 2. *Why this layer exists:* the PS1 only takes flat path lists; the Python wrapper lets you give it a folder full of binaries.

2. **The wrapper invokes the PS1 via `powershell -NoProfile -ExecutionPolicy Bypass -File ... -AllowPaths a,b,c -DenyPaths x,y -CompileBinary`.** Notice it passes the *file-name level* (`InternalName` by default — see 2.4). The wrapper does **not** pass `-i / -o`; it passes `-InputPolicy` / `-OutputPolicy` (PS1 parameter names).

3. **`Add-WDACRule.ps1` loads `base.xml` as XML and reads PE version-info for each binary.** For every binary it calls `[System.Diagnostics.FileVersionInfo]::GetVersionInfo($Path).$Level` — i.e. read `InternalName` (or whichever level you picked). *Why not the obvious `New-CIPolicyRule -Level FileName`?* The script's NOTES block explains: that ConfigCI cmdlet returns *"File does not have a SIP"* on unsigned binaries and prompts interactively, which is unusable for automation. Reading PE metadata directly sidesteps the cmdlet entirely.

4. **The PS1 inserts rules into the XML.** For each binary it adds:
   - one `<Allow>` (or `<Deny>`) element inside `<FileRules>`, of the form `<Allow ID="ID_ALLOW_A_N" FriendlyName="..." InternalName="Notepad"/>`,
   - one `<FileRuleRef RuleID="ID_ALLOW_A_N"/>` inside the UMCI signing scenario's `<ProductSigners><FileRulesRef>` block.
   
   IDs are auto-numbered: the script scans existing IDs matching `ID_ALLOW_A_(\d+)` / `ID_DENY_D_(\d+)` and continues from the highest. This is why running the tool twice doesn't collide.

5. **Version bump + date stamp.** `VersionEx` is parsed as four dotted ints and the last is incremented (`10.3.0.0` → `10.3.0.1`). `PolicyInfo.Id` (under `<Settings>`) is rewritten to today's date. *Why this matters:* WDAC refuses to load a `.cip` whose VersionEx is ≤ the currently-loaded one — bumping it is required for hot updates via `citool -r`.

6. **`-CompileBinary` → `ConvertFrom-CIPolicy -XmlFilePath out.xml -BinaryFilePath {PolicyID}.cip`.** This is the Microsoft cmdlet from the `ConfigCI` module; it converts the XML to the kernel-readable `.cip` binary. The output filename is hard-coded to `{PolicyID}.cip` because that's what the OS expects when scanning the Active folder for multi-policy format.

7. **`--deploy` (Python only)** copies `{PolicyID}.cip` to `C:\Windows\System32\CodeIntegrity\CIPolicies\Active\` and runs `echo . | citool -r`. The `echo . |` part is a workaround for `citool` pausing with "Press Enter to continue" on certain error paths. After this, the kernel re-reads `CIPolicies\Active` and the new policy takes effect — no reboot, thanks to `Update Policy No Reboot`. Per Microsoft's docs this is the canonical 11-22H2+ deploy path; on earlier Windows you'd use `RefreshPolicy.exe` instead.

### 2.3 What `base.xml` actually says

Important `<Rules>` settings (line 7+ of `base.xml`):

| Option | Effect |
|---|---|
| `Enabled:Unsigned System Integrity Policy` | The policy XML itself doesn't need to be code-signed. (If you remove this and sign the policy, removal requires booting to recovery; don't enable signing on the test box.) |
| `Enabled:UMCI` | Enforce on user-mode binaries (this is what blocks `.exe` startup). |
| `Enabled:Update Policy No Reboot` | Policy changes apply on `citool -r`, no reboot. |
| `Enabled:Allow Supplemental Policies` | Other `.cip` policies can extend this one without modifying it. (Not used by this tool today.) |
| `Required:Enforce Store Applications` | UMCI also applies to MSIX/Store apps, not just `.exe`. |
| (no `Enabled:Audit Mode`) | **Enforcement, not audit.** Blocked binaries actually get blocked. |

The `<Signers>` + `<SigningScenarios>` blocks list Microsoft signing roots (Windows production root, WHQL, store, etc.). Net effect of `base.xml` alone: only Microsoft-signed code is allowed in user mode. Everything else is implicitly denied. The CLI's job is to punch named-attribute holes in that policy for the apps you actually need.

### 2.4 Why "InternalName" and what the trade-off is

The `-FileNameLevel` parameter chooses which **PE version-info field** the rule keys off. Default is `InternalName`, alternatives are `OriginalFileName`, `FileDescription`, `ProductName`.

- **Pro:** the rule matches any binary that declares that string in its metadata, including future updates — you don't have to regenerate the policy when 7-Zip ships a new build.
- **Con:** anyone can edit PE version-info on an unsigned binary. An attacker who can drop a file on the host could rename their `evil.exe`'s `InternalName` to `"Notepad"` and ride your allow rule. For a true high-assurance setup you'd use signer-based rules instead; for an endpoint DLP gating use case (force users onto a known set of approved tools) attribute-based rules are the pragmatic choice.

If a target binary has no `InternalName` in its PE header, `Add-FileAttribRule` throws — pick another level via `-f OriginalFileName` (or `--file-name-level` in Python).

### 2.5 `citool` cheat sheet

From `learn.microsoft.com/.../citool-commands` (Windows 11 22H2+, all require elevated shell):

| Command | Alias | What it does |
|---|---|---|
| `citool --refresh` | `-r` | Re-read `CIPolicies\Active` and apply changes. |
| `citool --list-policies` | `-lp` | Dump every policy on disk (active or not). `-lp -json` for machine-readable. |
| `citool --update-policy <path>` | `-up` | Add/update a policy from `.cip`. (`--deploy` in this tool achieves the same end via direct copy + refresh.) |
| `citool --remove-policy {GUID}` | `-rp` | Remove a policy by `PolicyID`. |

> Heads-up: `citool -lp` from a non-elevated shell prints `0x80070005` and waits at "Press Enter to Continue" — that's a permissions failure, not a tool bug. Always run elevated.

### 2.6 What's *not* here (intentional gaps for an integrator)

This component is currently a stand-alone **policy author**. To be a real "channel" inside the agent it would need:

- A loop that watches a managed allow-list (today the lists are static files maintained by hand).
- A way to log block events back to the orchestrator (today blocks only land in the CodeIntegrity event log).
- A bootstrap path so the agent installer deploys an initial policy on first run.

None of those wires exist yet — confirming the user's statement that this channel is not integrated.

---

## 3. Verification status — what I ran vs. what I couldn't

| Step | Status | Notes |
|---|---|---|
| `python -h` on the wrapper | **Ran, passed** | Help text matches the source. |
| XML-only generation (`base.xml` → `out.xml`) | **Ran, passed** | One Allow + one Deny rule inserted; `VersionEx` bumped to `10.3.0.1`. |
| `-c` compile to `.cip` | **Ran, passed** | `ConfigCI` module is installed on the dev box; `{PolicyID}.cip` produced. |
| `ConvertFrom-CIPolicy` available | **Checked, present** | Module: `ConfigCI`. |
| `citool.exe` present | **Checked, present** | At `C:\Windows\System32\CiTool.exe`. |
| `citool -lp` (non-elevated) | **Ran, fails with 0x80070005** | Expected — requires elevation. |
| `--deploy` end-to-end on dev box | **Did NOT run** — would brick the dev environment. Test in the VM. |
| Block / allow behavior of a loaded policy | **Did NOT run** — same reason; needs the VM and elevation. |
| `citool --remove-policy` rollback | **Did NOT run** — only relevant after a real deploy. |

If any command in the **Quick test** section fails with `ConvertFrom-CIPolicy: The term ... is not recognized`, the `ConfigCI` PowerShell module is missing. See the **Setup** subsection at the top of section 1 — the install procedure differs by edition. On Pro/Enterprise it's `Add-WindowsCapability` against `Rsat.CodeIntegrity.Tools~~~~0.0.1.0`; on Home (which doesn't expose that capability) it's the DISM `.mum` activation trick.

---

## Sources

OS-level behavior described in this guide is grounded in the following Microsoft Learn pages (fetched while writing):

- [Managing CI policies and tokens with CiTool — `citool` command reference, `--refresh` / `--list-policies` / `--update-policy` / `--remove-policy`, output attributes (`IsEnforced`, `IsAuthorized`, `Policy is Signed`)](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/citool-commands)
- [Deploy App Control for Business policies using script — the `CIPolicies\Active` deployment path, `.cip` filename convention `{PolicyID}.cip`, refresh via `CiTool --update-policy` / `RefreshPolicy.exe`, and the signed-base-policy reboot caveat on pre-24H2 Windows 11](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/deployment/deploy-appcontrol-policies-with-script)

Everything else (rule schema, ID numbering, `VersionEx` bump, `InternalName` choice and its trade-off, the `ConfigCI` `New-CIPolicyRule -Level FileName` "no SIP" issue) is grounded directly in the source files under `interceptors/app_control/cli/`.
