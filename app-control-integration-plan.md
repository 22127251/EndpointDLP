# App Control (WDAC) Channel — General Phased Integration Plan

## Context

The repo contains a stand-alone WDAC (Windows Defender Application Control, now "App Control for Business") policy-authoring tool at `interceptors/app_control/cli/` — `base.xml` (an unsigned, enforcement-mode base policy, PolicyID `{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}`, UMCI on, `Update Policy No Reboot` on, allows all Microsoft-signed code), `Add-WDACRule.ps1` (inserts `<Allow>`/`<Deny>` file rules keyed on PE version-info attributes, bumps `VersionEx`, optionally compiles to `.cip` via `ConvertFrom-CIPolicy`), and `add-wdacwrule.py` (CLI wrapper: list files, recursive folder scan, optional deploy via copy to `CIPolicies\Active\` + `citool -r`).

Today this tool has **zero references from the agent** — nothing watches the lists, deploys policies at runtime, or reports blocks. Blocks land only in the CodeIntegrity event log. This plan turns it into a real channel of the DLP agent (whose Phases A–F are complete: LocalSystem `DLPAgent` service, sectioned `config.yaml` + ctl-pipe hot-reload, admin-pipe + `dlp-ctl`, `events.jsonl` audit log, transactional installer, clean-VM deployable bundle).

**Vision constraint:** a future central management server will push policies. The server builds both the WDAC XML *and* the compiled `.cip` (server-side compile prevents agent downtime if compile breaks); the agent just receives and deploys. The server itself is out of scope, but the agent-side contract must be ready for it. The agent also needs a **standalone mode** (no server) with the same admin workflow, driven locally via `dlp-ctl`.

## Locked decisions (from this planning session)

| # | Decision |
|---|---|
| 1 | **Standalone compile on-endpoint.** Win11 Home lacks the ConfigCI PowerShell module by default; the installer DISM-enables it offline (`gci $Env:SystemRoot\servicing\Packages\*ConfigCI*.mum | % { dism /online /norestart /add-package:"$_" }` — verified working on Home 23H2+). `ConvertFrom-CIPolicy` then runs locally. |
| 2 | **Single deployment path: inbox drop-folder.** `%ProgramData%\DLP\appcontrol\inbox\` receives `{policy.xml, {PolicyID}.cip, manifest.json}` (version + SHA-256 hashes). An orchestrator watcher loop validates and deploys whatever lands there. The future server transport writes here; standalone `dlp-ctl` writes here. No managed/standalone mode flag exists — standalone *is* "admin uses dlp-ctl locally". |
| 3 | **Self-protect = validate-and-reject.** The agent cannot edit a binary `.cip`, so it parses the pushed XML and **refuses to deploy** (loud log + events.jsonl record) if allow rules for the agent's own binaries are missing. The server contract documents this requirement. The standalone builder always merges self-protect rules in automatically. |
| 4 | **Removal / emergency disable.** Primary: `CiTool --remove-policy {GUID}` — since Win11 24H2 this removes unsigned policies **without reboot** (web-verified; the test VM is build 26200). Verify via `citool --list-policies`. Fallback for older builds: "neutralizer" update — same PolicyID, higher VersionEx, allow-all rules (immediate effect), then delete the `.cip` so the policy is gone after next boot. `dlp-ctl appcontrol disable` and uninstall share this code path. |
| 5 | **Exe with no usable PE version info** → fall back to a WDAC Hash rule + warning (hash rules break on app update, but the file stays covered). |
| 6 | **Block-event feedback** → subscribe via pywin32 `win32evtlog.EvtSubscribe` (push callback) + `EvtRender` XML; write to `events.jsonl` as channel `"app_control"`. pywin32 is already in the bundled embed. **CORRECTED BY AC-1:** primary channel is `Microsoft-Windows-CodeIntegrity/Operational` with **3077** (enforce-block) / **3076** (audit-block — NOT 8028); 8028/8029 + packaged-app 8039/8040 live in `Microsoft-Windows-AppLocker/MSI and Script` (subscribe as insurance — MSIX blocks empirically surfaced as 3077, with `PackageFamilyName` in the payload). Filter on the event Data field **`PolicyGUID`** (braced lowercase GUID) — the `PolicyID` Data field carries the policy's `Settings\Id` string, not the GUID. Exact payload + `citool --list-policies --json` schema pinned in `interceptors/app_control/spike-results/RESULTS.md`. |
| 7 | **Channel is config-less at install.** Installer creates directories and (at uninstall) strips everything, but deploys **no** policy. The channel sits idle until the first inbox push / dlp-ctl apply. |
| 8 | **Required behavior test** (admin collects exes into a folder to build the cip): (a) prove a `.cip` built from *copied* exes correctly allows/denies the originally-installed apps (PE metadata travels with a copy); (b) demonstrate the *installer-exe vs installed-app* InternalName mismatch (allowing `7z2601-x64.exe` does not allow the installed `7zFM.exe`) and document it as an admin-workflow caveat, with a builder warning for installer-like names. |
| 9 | No audit-first staging workflow. Forward only our policy's block events (no foreign-policy noise, e.g. Smart App Control). |

## Architecture (applies to all phases)

- **The channel lives inside the orchestrator** as a new package `orchestrator/app_control/` with daemon threads — *not* a supervised child process. Reason: deploying to `C:\Windows\System32\CodeIntegrity\CIPolicies\Active\`, running `citool`, and `EvtSubscribe` all need high privilege, and the orchestrator service already runs as LocalSystem. No `supervisor.py` changes. It also never talks to the analyzer — app control is policy enforcement + event reporting, not content analysis, so the data-pipe/dispatcher/DLPEngine are untouched.
- **Port `Add-WDACRule.ps1`'s XML logic to Python** (`win32api.GetFileVersionInfo` replaces `[System.Diagnostics.FileVersionInfo]`; `xml.etree` manipulation of the `urn:schemas-microsoft-com:sipolicy` namespace replaces the PS XML DOM). Reason: the self-protect *validator* (decision 3) must be Python anyway — porting avoids maintaining rule-shape logic in two languages, and pure-Python logic is testable in the existing pytest harness. PowerShell remains only for `ConvertFrom-CIPolicy` (no Python equivalent), invoked as `powershell -NoProfile -Command ...`.
- **State layout:** `%ProgramData%\DLP\appcontrol\{inbox, rejected, staging, allow-list.txt, deny-list.txt}`; deployed-state record `%ProgramData%\DLP\state\appcontrol_status.json` (policy GUID, deployed VersionEx, deploy timestamp, last error, block counters) written atomically (temp + rename, same practice as the install manifest).
- **Fail-safe invariant:** any inbox/validation/deploy/event-subscription failure is caught, logged, recorded in events.jsonl, and the offending push moved to `rejected/<timestamp>/`. The orchestrator never crashes on a bad push and never deploys on doubt.

## Phases

Each phase gets its own detailed planning session (this file intentionally stays at architecture level). Dependency order: **AC-1 and AC-2 can run in parallel**; AC-1 feeds verified command recipes into AC-3/AC-5; AC-2 → AC-3 → AC-4 → AC-5.

### Phase AC-1 — VM semantics spike (de-risk; no agent code) ✅ COMPLETED (2026-06-11)

**Outcome:** all 22 matrix rows pass on the Win11 Home 26200 VM — DISM ConfigCI enable (12 .mum, no reboot), on-Home compile, deploy/refresh/remove/neutralizer recipes verified, decisions 8a/8b demonstrated, deny-beats-Store-allow proven on olk.exe, live SYSTEM event capture under enforcement (plan A) worked. Deliverables: runbook + matrix + pinned contracts in `interceptors/app_control/spike-results/RESULTS.md`, raw event XML / citool JSON / version-info artifacts alongside, keeper scripts `scripts/spike-{evt-subscribe.py, versioninfo-dump.ps1, neutralize-policy.ps1, ac1-stage.ps1}` + `scripts/spike-lists/`. Key extra findings for AC-2/AC-4: path lists must never be folders nor travel on a command line (32K limit, WinError 206); generic InternalNames (OneDrive = `Client Application`) make dangerous deny rules → warn; `pcre2-8.dll` (no version info at all) confirms the decision-5 hash-fallback need; `getpass.getuser()` as LocalSystem returns the machine account, not `SYSTEM`.

**Goal:** convert every OS-level assumption into a verified command recipe + recorded artifacts, using the *existing* CLI tool on the Win11 Home 26200 VM. Nothing here touches the dev machine's policy state.

Scope: (a) DISM offline ConfigCI enable on Home, confirm `ConvertFrom-CIPolicy` works after, no reboot; (b) deploy recipe — copy `.cip` + `citool --refresh`, capture exact `citool --list-policies` output format (the AC-3 parser keys on it; `-json` flag if available); (c) removal — `citool --remove-policy {GUID}` no-reboot on 26200, and rehearse the neutralizer fallback once end-to-end; (d) decision 8a/8b behavior tests (copied-exe policy governs originals; installer-vs-installed mismatch demo); (e) event feed — small pywin32 `EvtSubscribe` script run **as SYSTEM** (matching service context), launch a denied exe, save the rendered 3077 event XML to pin the exact field names (PolicyGUID/PolicyName naming varies by build) the AC-3 filter will use.

Deliverable: a results matrix (command, exit code, works/doesn't) + sample event XML. Keeper scripts land in `scripts/`.

Risks: ConfigCI `.mum` absent on some servicing states; 3077 schema variance; GUID format pickiness in `--remove-policy`.

### Phase AC-2 — Python WDAC policy engine (pure logic, harness-testable)

**Goal:** all XML rule manipulation + validation as pure Python, no OS side effects, fully pytest-covered.

New files: `orchestrator/app_control/{policy_xml.py, selfprotect.py, manifest.py, base.xml}` (+ `__init__.py`); tests `scripts/harness/test_app_control_policy.py`.

- `policy_xml.py`: port of `Add-WDACRule.ps1` — load `base.xml` template, PE version-info via `win32api.GetFileVersionInfo` (default level InternalName, parity with the PS1), insert Allow/Deny FileRules + FileRuleRefs into UMCI SigningScenario 12, `ID_ALLOW_A_n`/`ID_DENY_D_n` auto-numbering with dedup, VersionEx bump (WDAC refuses VersionEx ≤ loaded), hash-rule fallback (decision 5), installer-like-name warning (decision 8b).
- `selfprotect.py`: generates the agent-binary allow rules from the install layout (python.exe, interceptor exes, TransferAgent, etc.).
- `manifest.py`: inbox manifest schema + validator suite — SHA-256 hash check, PolicyID↔`.cip` filename match, VersionEx > currently-deployed, self-protect coverage check (decision 3). Pure functions shared by orchestrator runtime and tests.
- `base.xml` relocates here as package data (the installer copies `orchestrator/` wholesale, so it ships for free).

Open question for the phase session: WDAC Hash rules use the **Authenticode/PE CI hash, not flat SHA-256** — implement Authenticode hashing in Python (keeps the no-PowerShell property; cross-check once against a `New-CIPolicyRule -Level Hash` sample on the VM) vs shelling out to PS for just those files.

Risks: sipolicy element-ordering strictness; behavioral parity with the PS1 (cross-check: Python-built XML must compile with `ConvertFrom-CIPolicy` and diff semantically clean against PS1 output for identical inputs).

### Phase AC-3 — In-orchestrator runtime: inbox watcher, deployer, event forwarder

**Goal:** the channel runs as threads inside `run_core`; a valid inbox push gets deployed; our blocks flow to `events.jsonl`.

New files: `orchestrator/app_control/{channel.py, inbox.py, deployer.py, event_forwarder.py}`; tests `scripts/harness/test_app_control_inbox.py`.
Modified: `orchestrator/__main__.py` (start/stop the channel in `run_core`, alongside the admin server; extend `_status_provider()` with an `app_control` block), `orchestrator/config.py` (defaulted `app_control` fields so existing test fixtures don't break — same convention as the Phase D path fields), `orchestrator/events.py` (new emitter, e.g. `record_app_control_event(...)` — `record_decision`'s signature is content-analysis-shaped and doesn't fit), `config.yaml` (new `app_control:` section: `enabled`, dir overrides, forwarder toggle).

- `channel.py`: `AppControlChannel` facade (start/stop), hooked into `run_core`'s config-change handler so the `app_control:` section hot-reloads. **No ctl-pipe / `_KNOWN_COMPONENTS` change** — there is no external child client.
- `inbox.py`: watcher loop on `inbox\` — **2–5 s poll recommended over watchdog** (robust against partial writes). Atomic-pickup protocol: manifest written *last* gates completeness. Validate via AC-2 `manifest.py`; on failure move to `rejected/`, emit event, continue. Serialize pickups with a lock (push arriving mid-deploy).
- `deployer.py`: copy `.cip` to `CIPolicies\Active\` → `citool --refresh` → confirm via `--list-policies` parse (AC-1 recipe); `remove()` = `citool --remove-policy` + verify, neutralizer fallback (decision 4) — **shared by `dlp-ctl appcontrol disable` (AC-4) and uninstall (AC-5)**; persist `appcontrol_status.json` atomically after every transition; if refresh fails, keep previous status (never half-update).
- `event_forwarder.py`: `EvtSubscribe` push callback on CodeIntegrity/Operational, filter 3077/8028 by our PolicyID (field names from AC-1), forward as channel `"app_control"`. Callback wrapped so exceptions never propagate into pywin32; subscription handle held for the channel lifetime; clean stop ordering before pipes close.

Verification: harness tests with a fake citool runner injected into `deployer` (happy path, each rejection class, status persistence, garbage-push crash resistance — the per-test `%PROGRAMDATA%` conftest pattern fits); then a `--foreground` run on the VM: one real deploy, one real block visible in `events.jsonl`.

### Phase AC-4 — dlp-ctl authoring workflow + admin-pipe commands

**Goal:** full standalone operator workflow: `dlp-ctl appcontrol allow|deny|build|apply|status|disable`.

New: `orchestrator/app_control/builder.py` (shared build logic; keeps `ctl.py` thin and import-light — same constraint that shaped ctl.py's lazy imports). Modified: `orchestrator/ctl.py` (subparser), `orchestrator/admin_server.py` (`handle_request` routing for `appcontrol_status` / `appcontrol_disable` — constructor gains callbacks, same pattern as `status_provider`/`reload_callback`; consider generalizing to a command-callback dict), `orchestrator/__main__.py` (callback wiring). Tests: `scripts/harness/test_app_control_ctl.py`.

- `allow <path...>` / `deny <path...>` (+ `--remove`): folders scanned recursively for executables (mirrors `add-wdacwrule.py`'s rglob); maintains `%ProgramData%\DLP\appcontrol\{allow,deny}-list.txt`.
- `build`: runs the AC-2 engine (always merging self-protect rules), reads deployed VersionEx from `appcontrol_status.json` to bump above it, compiles via PowerShell `ConvertFrom-CIPolicy` (preflight-checks ConfigCI, errors with the DISM guidance if absent), writes XML+cip+manifest into `staging/`.
- `apply`: atomic move staging → inbox (manifest last) — the explicit "go live" gate; the orchestrator's watcher does the rest.
- `status`: via admin pipe (deployed GUID/version, last deploy, block counters, pending/rejected counts).
- `disable`: via admin pipe → orchestrator runs `deployer.remove()`; plus `--force-local` fallback that drives citool directly for the "service is dead" emergency — the escape hatch must not depend on a healthy agent.

Verification: harness tests (list management, request routing, build path with the PS compile step stubbed); manual VM loop: allow folder → build → apply → deploy → denied app blocked / allowed app runs → status shows version + counts → disable → app runs again.

### Phase AC-5 — Installer/bundle integration + clean-VM end-to-end

**Goal:** the full agent installs on a clean Win11 Home VM with no dev tools; channel idle until first push; everything stripped at uninstall.

Modified: `orchestrator/installer.py` (new `Step`s in `_build_default_steps()`), `scripts/package-bundle.ps1` (mostly free — `orchestrator/` is copied wholesale; confirm the generated bundle `config.yaml` carries `app_control:`); tests extend `scripts/harness/test_installer.py` (citool/DISM runners stubbed).

New installer steps:
1. `appcontrol_dirs`: create `%ProgramData%\DLP\appcontrol\{inbox, rejected, staging}`; undo removes them (`_rmtree_with_retry` pattern).
2. `enable_configci`: the DISM `*ConfigCI*.mum` loop (`/norestart`); undo = deliberate no-op (leaving ConfigCI enabled is benign; recorded in manifest).
3. `appcontrol_policy_guard`: `do` just records the policy GUID in the manifest; `undo` runs `deployer.remove()` — so **uninstall strips any deployed policy even though install deploys nothing** (decision 7), with crash-safe manifest replay for free. Step-list placement matters (undos run in reverse): policy removal must happen after service stop and while the bundled Python still exists — plan exact ordering in the phase session.

Formal decision-8 acceptance on the VM: (a) copied-exe-built `.cip` governs originally-installed apps; (b) installer-vs-installed mismatch demonstrated + builder warning shown.

Risks: DISM during install on a fresh image (servicing stack busy → retry); uninstall sequencing — a deployed deny must never block the uninstaller itself (self-protect rules + base.xml's Microsoft-signed allowance cover the toolchain).

Verification (clean-VM acceptance run): package → install → `citool --list-policies` shows **no** DLP policy + channel idle → full AC-4 operator loop → block events in `events.jsonl` → disable → re-apply → uninstall → policy gone, dirs gone, reinstall succeeds. `pytest scripts/harness` stays green throughout (baseline: 35 passed / 3 skipped + 10 C#).

## Web-verified facts (this session)

- ConfigCI module DISM-enable works on Win11 Home 23H2+ ([MS Learn ConfigCI](https://learn.microsoft.com/en-us/powershell/module/configci/), [valinet/ssde#9](https://github.com/valinet/ssde/issues/9)).
- `CiTool --remove-policy` removes **unsigned** policies without reboot from Win11 24H2 onward; earlier builds need a restart ([MS Learn: remove App Control policies](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/deployment/disable-appcontrol-policies), [CiTool commands](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/citool-commands)).
- pywin32 `win32evtlog.EvtSubscribe` supports push-callback subscriptions; `EvtRender` with `EvtRenderEventXml` yields the event XML ([pywin32 docs](https://timgolden.me.uk/pywin32-docs/win32evtlog__EvtSubscribe_meth.html)).
- Event 3077/8028 payloads carry PolicyName + PolicyGUID fields for per-policy filtering ([MS Learn: App Control event IDs](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/event-id-explanations)).

## Cross-cutting open questions (settle in per-phase sessions)

1. **AC-2:** Authenticode-hash-in-Python vs `New-CIPolicyRule -Level Hash` shell-out for the no-metadata fallback.
2. ~~**AC-1 output → AC-3:** exact 3077/8028 field names on build 26200.~~ **RESOLVED (AC-1):** see `spike-results/RESULTS.md` "Pinned contracts" — filter on `PolicyGUID`; field names contain spaces; 3076 is the audit event; MSIX blocks are 3077 + `PackageFamilyName`.
3. **AC-4:** admin-server callback wiring shape (more positional callbacks vs a command dict).
4. **AC-5:** `appcontrol_policy_guard` undo placement relative to service-stop/file-removal in the step list.
