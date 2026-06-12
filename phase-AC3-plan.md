# Phase AC-3 — In-orchestrator runtime: inbox watcher, deployer, event forwarder: Detailed Plan

## Status — ✅ COMPLETE (VM-confirmed 2026-06-12)

All dev-side work is implemented and green, and the VM end-to-end run on the installed
`DLPAgent` service (build 26200) **passed**: a good inbox push auto-deployed, denied
`olk.exe`/`OneDrive.exe` produced forwarded 3077 `block` events in `events.jsonl`, a bad
push (sha256 mismatch) was rejected to `rejected\`, and removal returned cleanly to
baseline. Full harness **93 passed / 3 skipped** (69 prior + 24 AC-3 cases; the 3 skips
are the pre-existing elevation-gated admin-pipe tests). Neutralizer + VM-test pushes
compile clean (`ConvertFrom-CIPolicy`).

**Post-VM follow-up (applied + VM-confirmed):** `appcontrol_status.json` is now
**self-healing**. The deployer `reconcile()`s the record against live `citool
--list-policies` (using the **absolute** `citool` path — a bare name didn't always
resolve in the LocalSystem service; strict + observable, so a citool failure is written
to `last_error` and dlp-agent.log instead of silently bailing) on the watcher's first
tick, throttled every `reconcile_interval_seconds` (default 30), **and on-demand on every
`dlp-ctl status`** (authoritative). So an out-of-band `citool --remove-policy` no longer
lingers in `dlp-ctl status` — it now reads `app-control : no policy …`. Block counters
reset on policy clear (full history stays in `events.jsonl`). Neutralizer builder +
committed artifact + reconcile (clear/adopt) now have unit tests.

> Root cause of the first VM retry "bug remains": the updated `orchestrator\` code had not
> been copied to `C:\Program Files\DLP\orchestrator\` — restarting the service re-ran the
> old build. Re-patching the installed tree + restart resolved it.

Shipped:
- `orchestrator/app_control/{channel,inbox,deployer,event_forwarder,neutralizer}.py`
  + committed `neutralizer.{xml,cip}` (package data).
- Edits: `orchestrator/{__main__,config,events,ctl}.py`, `config.yaml`,
  `scripts/harness/conftest.py`, `scripts/harness/test_admin.py`.
- Tests: `scripts/harness/test_app_control_inbox.py` (21 cases).
- Keepers: `scripts/build-neutralizer.py`, `scripts/build-ac3-inbox.py`.

## Context

AC-1 (VM spike) and AC-2 (pure-logic WDAC engine + manifest validators) are complete.
AC-3 makes the channel *run* inside the orchestrator service: three daemon-thread
subsystems in `run_core` — an **inbox watcher**, a **citool deployer**, and a
**CodeIntegrity event forwarder** — plus config/status/event plumbing. A valid push in
`%ProgramData%\DLP\appcontrol\inbox\` is validated + deployed by the running `DLPAgent`
service; every block our policy produces flows to `events.jsonl` as channel
`"app_control"`. The `.cip` arrives **already compiled** (parent decision 2) — the
deployer copies + refreshes, never `ConvertFrom-CIPolicy`.

## Decisions (this session)

| # | Decision |
|---|---|
| Q1 | **Full neutralizer in AC-3.** `deployer.remove()` = primary `citool --remove-policy` + verify, **and** an AllowAll-neutralizer fallback that deploys the committed, **pre-compiled** `neutralizer.cip` (PolicyID = our GUID, VersionEx `65535.65535.65535.65535`) + refresh, then deletes the active `.cip` (gone after reboot). No runtime ConfigCI/compile dependency. |
| Q2 | **Deny both `olk.exe` + `OneDrive.exe`** in the VM block test (`build-ac3-inbox.py`). |
| Q3 | **VM verification on the installed `DLPAgent` service** (LocalSystem). |

Inherited: decision 2 (pre-compiled `.cip`), 3 (validate-and-reject self-protect), 4
(remove + neutralizer), 6 (CI/Operational primary; filter on `PolicyGUID`; 3077
enforce / 3076 audit; AppLocker insurance), 7 (idle until first push), fail-safe
invariant.

## Architecture

```
run_core (orchestrator/__main__.py)
  └─ AppControlChannel (channel.py)          start/stop/status/apply_config; resolves dirs, mkdir
       ├─ InboxWatcher  (inbox.py)           poll inbox\ (subfolder pushes, size-stable pickup) → validate_all → Deployer.deploy / reject
       ├─ Deployer      (deployer.py)        copy .cip→Active\ + citool refresh/verify; remove()+neutralizer; appcontrol_status.json (atomic)
       └─ EventForwarder(event_forwarder.py) EvtSubscribe CI(+AppLocker) → filter PolicyGUID → record_app_control_event + note_block
```

- Daemon threads in `run_core`, not supervised children (no `supervisor.py` change); never
  touches the analyzer. Gated by `config.app_control_enabled` and `DLP_APPCONTROL_DISABLED`
  (the harness opt-out — the channel needs LocalSystem privilege).
- State: `%ProgramData%\DLP\appcontrol\{inbox,rejected,staging}\` (channel `start()` mkdirs);
  `%ProgramData%\DLP\state\appcontrol_status.json` (atomic temp+`os.replace`). Blocks →
  `events.jsonl`.
- Event ownership: deployer emits `deploy`/`remove`/`neutralize`; watcher emits `reject`;
  forwarder emits `block`. Records are flat: `{ts,channel:"app_control",event,outcome,...}`.

## Key behaviors

- **Inbox** — push = a subfolder `{policy.xml,{GUID}.cip,manifest.json}`. Pickup when
  `manifest.json` present **and** sizes stable across two polls (tolerates non-atomic copy).
  Validates via AC-2 `manifest.validate_all` + a single-PolicyID guard; clean → deploy +
  consume; failing (or failed deploy) → moved to `rejected\<utc>_<name>\`. One bad push never
  kills the loop.
- **Deployer** — injectable `runner` (citool isolated; tests never shell out). Refresh failure
  restores the prior on-disk `.cip` and keeps the previous status (never half-applies).
  `note_block` bumps persisted enforce/audit counters.
- **Forwarder** — push-callback `EvtSubscribe` (modelled on `scripts/spike-evt-subscribe.py`);
  pure `parse_block_event` filters on the `PolicyGUID` data field (braced lowercase) and maps
  3077→`blocked`, 3076→`audit`. Callback swallows every exception. `start()` is best-effort.

## Files

New: `orchestrator/app_control/{channel,inbox,deployer,event_forwarder,neutralizer}.py`,
`orchestrator/app_control/neutralizer.{xml,cip}`, `scripts/{build-neutralizer,build-ac3-inbox}.py`,
`scripts/harness/test_app_control_inbox.py`.

Modified: `orchestrator/config.py` (defaulted `app_control_*` fields + `load_config`),
`orchestrator/events.py` (`record_app_control_event`), `orchestrator/__main__.py` (start/stop
channel, `_status_provider` block, `_handle_config_change` → `apply_config`),
`orchestrator/ctl.py` (display-only `app-control` status line), `config.yaml` (`app_control:`
section), `scripts/harness/conftest.py` (`DLP_APPCONTROL_DISABLED`), `scripts/harness/test_admin.py`
(asserts the `app_control` status key).

## Verification

### Dev-side — all green (run on dev, .venv, Python 3.13.13)

| Step | Command | Result |
|---|---|---|
| V1 config loads | `python -c "from orchestrator.config import load_config as L; c=L(); print(c.app_control_enabled,c.app_control_poll_seconds)"` | `True 3` |
| V2 AC-3 unit tests | `python -m pytest scripts/harness/test_app_control_inbox.py -v` | **24 passed** |
| V3 full harness | `python -m pytest scripts/harness` | **93 passed / 3 skipped** |
| V4 neutralizer compiles | `python scripts/build-neutralizer.py` | `neutralizer.cip` (744 B), exit 0 |
| V5 VM pushes build+validate | `python scripts/build-ac3-inbox.py` | good `.cip` (3216 B), `validate_all(good)→[]`, `validate_all(bad)→['hash_mismatch']` |
| V6 bundle carries app_control | `build_bundle_config("config.yaml", <tmp>)` then read | `app_control:` present, `enabled: true` |

### VM-side (installed `DLPAgent` service) — user-executed

`{GUID}` = `{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}`. `%AC%` = `C:\ProgramData\DLP\appcontrol`.
Every citool call elevated, wrapped `cmd /c "echo . | citool … --json"` (AC-1 stdin finding).

**Dev prep (on dev):** `python scripts/build-ac3-inbox.py` → `tmp\ac3\{good,bad}\`; package the
bundle (`scripts/package-bundle.ps1`) — it carries the AC-3 code + `app_control:` config + the
committed `neutralizer.cip`.

**Prerequisites:**
- **P1** .NET 10 Desktop Runtime present (`dotnet --list-runtimes` → `Microsoft.WindowsDesktop.App 10.x`).
- **P2** VMware snapshot `pre-AC3` taken now.
- **P3** AC-3 bundle installed (`install.cmd` as admin); `Get-Service DLPAgent` → Running; the
  channel created `%AC%\{inbox,rejected,staging}` (verify they exist) with **no** policy deployed.
- **P4** copy `tmp\ac3\good` and `tmp\ac3\bad` to the VM, staged outside the inbox.

| # | Action | Pass check | Rollback |
|---|---|---|---|
| **S0** baseline | `cmd /c "echo . \| citool --list-policies --json"` ; `dlp-ctl status` ; `Get-Content C:\ProgramData\DLP\logs\events.jsonl -Tail 5` | no DLP `{GUID}`; `app-control: ... no policy`; dirs exist | — |
| **S1** drop good push | copy the `good` **subfolder** into `%AC%\inbox\` (manifest arrives last) | within ~poll_seconds the subfolder is consumed (gone from inbox) | S6 |
| **S2** observe deploy | `dlp-ctl status` ; `cmd /c "echo . \| citool --list-policies --json"` ; tail events.jsonl | status shows deployed `{GUID}` + VersionString; list shows `{GUID}` `IsEnforced:true`; events.jsonl has `{"event":"deploy","outcome":"ok"}` | S6 |
| **S3** trigger blocks | launch `olk.exe` and `OneDrive.exe` | both blocked (don't open) | S6 |
| **S4** observe forward | tail events.jsonl ; `dlp-ctl status` | `"event":"block","outcome":"blocked"` lines for olk + OneDrive with `file`/`process`/`policy_guid`; status block counters incremented | S6 |
| **S5** rejection path | copy the `bad` subfolder into `%AC%\inbox\` | moved to `%AC%\rejected\<…>\`; events.jsonl has `"event":"reject"` `hash_mismatch`; deployed policy unchanged | S6 |
| **S6** teardown | `cmd /c "echo . \| citool --remove-policy ""{GUID}"" --json"` ; `cmd /c "echo . \| citool --list-policies --json"` ; `Restart-Service DLPAgent` | `Operation Successful`, no reboot; `{GUID}` gone; olk/OneDrive run again; `dlp-ctl status` → no policy | snapshot `pre-AC3` |

**Passes when:** S2 auto-deploys, S4 shows forwarded blocks in `events.jsonl`, S5 rejects without
disturbing the deployed policy, S6 returns cleanly to baseline. **Rollback ladder (AC-1-proven,
no reboot):** `citool --remove-policy "{GUID}"` → shipped `neutralizer.cip` + refresh → snapshot.

> `deployer.remove()` (incl. the neutralizer fallback) is unit-tested in AC-3 and driven for real by
> AC-4's `dlp-ctl appcontrol disable`; the S6 teardown calls `citool --remove-policy` directly.

## Out of scope / downstream
- **AC-4** — `dlp-ctl appcontrol allow|deny|build|apply|status|disable`, `builder.py`, admin-pipe
  `appcontrol_status`/`appcontrol_disable`. AC-3 ships `deployer.remove()` + the `_status_provider`
  block they use.
- **AC-5** — installer `appcontrol_dirs`/`enable_configci`/`policy_guard`; clean-VM acceptance.
  Carry-forward: deployed/built policies must include the dotnet FilePath self-protect rule
  (enforced by `manifest.validate_all`); the installer must keep `<install_root>` admin-only-writable.
