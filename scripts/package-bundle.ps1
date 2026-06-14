# Phase E packaging: assemble a lean, self-contained, VM-ready deploy bundle.
#
# Produces dist\DLP\ (a folder laid out as the install tree) and dist\DLP.zip.
# Drop either on a test VM and run install.cmd as Administrator — the VM needs
# NO Visual Studio / Developer PowerShell / dotnet / system Python; everything
# runs on the bundled embed Python + built-in Windows tools (certutil/sc/reg).
#
# Prerequisites (run these first, on the host):
#   1. scripts\prepare-install-payload.ps1   (Developer PowerShell — builds C#/C++ artifacts)
#   2. scripts\prepare-python-embed.ps1       (normal PowerShell — builds python-embed\ with deps)
#
# Run from a normal PowerShell at repo root:
#   .\scripts\package-bundle.ps1

[CmdletBinding()]
param(
    [ValidateSet('Debug','Release')]
    [string]$Configuration = 'Debug',
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$DistRoot = (Join-Path (Resolve-Path "$PSScriptRoot\..").Path "dist")
)

$ErrorActionPreference = 'Stop'

$BundleDir = Join-Path $DistRoot "DLP"
$ZipPath   = Join-Path $DistRoot "DLP.zip"
$EmbedDir  = Join-Path $RepoRoot "python-embed"
$VenvPy    = Join-Path $RepoRoot ".venv\Scripts\python.exe"

Write-Host "Packaging DLP deploy bundle ($Configuration)" -ForegroundColor Cyan
Write-Host "  RepoRoot:  $RepoRoot"
Write-Host "  BundleDir: $BundleDir"

# ── Prerequisite checks ──────────────────────────────────────────────────────
if (-not (Test-Path (Join-Path $EmbedDir "python.exe"))) {
    throw "python-embed\python.exe not found. Run scripts\prepare-python-embed.ps1 first."
}
if (-not (Test-Path $VenvPy)) {
    throw ".venv not found at $VenvPy. The dev .venv is needed to generate the bundle config."
}

# Source publish dirs (build outputs) the installer copies whole.
$ControllerSrc   = Join-Path $RepoRoot "interceptors\peripheral_storage\Controller\bin\$Configuration\net10.0-windows\win-x64"
$ClipboardSrc    = Join-Path $RepoRoot "src\ClipboardInterceptor\bin\$Configuration\net10.0-windows"
$TransferSrc     = Join-Path $RepoRoot "interceptors\peripheral_storage\TransferAgent\bin\$Configuration\net10.0-windows\win-x64"
$ShellExtDll     = Join-Path $RepoRoot "interceptors\peripheral_storage\out\ShellExtension\$Configuration\DlpShellExt.dll"
$PayloadDll      = Join-Path $RepoRoot "interceptors\peripheral_storage\Payload\x64\$Configuration\Payload.dll"

$RequiredArtifacts = @(
    (Join-Path $ControllerSrc "UsbDlpController.exe"),
    (Join-Path $ClipboardSrc  "ClipboardInterceptor.exe"),
    (Join-Path $TransferSrc   "DlpTransferAgent.exe"),
    $ShellExtDll,
    $PayloadDll
)
$MissingArtifacts = $RequiredArtifacts | Where-Object { -not (Test-Path $_) }
if ($MissingArtifacts) {
    Write-Host "Missing build artifacts:" -ForegroundColor Red
    $MissingArtifacts | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    throw "Run scripts\prepare-install-payload.ps1 (Developer PowerShell) first."
}

# ── Helpers ──────────────────────────────────────────────────────────────────
function Invoke-Robocopy {
    param([string]$Src, [string]$Dst, [string[]]$Extra = @())
    $argv = @($Src, $Dst, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP') + $Extra
    robocopy @argv | Out-Null
    # robocopy exit codes 0-7 are success; >=8 is a real failure.
    if ($LASTEXITCODE -ge 8) { throw "robocopy '$Src' -> '$Dst' failed (exit=$LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
}

# ── Clean + assemble ─────────────────────────────────────────────────────────
if (Test-Path $BundleDir) { Remove-Item -Recurse -Force $BundleDir }
New-Item -ItemType Directory -Force $BundleDir | Out-Null

$SrcExcludeDirs  = @('/XD', '__pycache__', '.pytest_cache')
$SrcExcludeFiles = @('/XF', '*.pyc', '*.pyo')

Write-Host "Copying Python source trees + embed ..." -ForegroundColor Yellow
Invoke-Robocopy (Join-Path $RepoRoot "orchestrator")          (Join-Path $BundleDir "orchestrator")          ($SrcExcludeDirs + $SrcExcludeFiles)
Invoke-Robocopy (Join-Path $RepoRoot "analyzer")              (Join-Path $BundleDir "analyzer")              ($SrcExcludeDirs + $SrcExcludeFiles)
Invoke-Robocopy (Join-Path $RepoRoot "interceptors\browser")  (Join-Path $BundleDir "interceptors\browser")  ($SrcExcludeDirs + $SrcExcludeFiles)
Invoke-Robocopy $EmbedDir                                     (Join-Path $BundleDir "python-embed")          ($SrcExcludeDirs + $SrcExcludeFiles)

Write-Host "Staging binaries into bin\ ..." -ForegroundColor Yellow
Invoke-Robocopy $ControllerSrc (Join-Path $BundleDir "bin\Controller")
Invoke-Robocopy $ClipboardSrc  (Join-Path $BundleDir "bin\Clipboard")
Invoke-Robocopy $TransferSrc   (Join-Path $BundleDir "bin\TransferAgent")

$ShellExtDst = Join-Path $BundleDir "bin\ShellExt"
New-Item -ItemType Directory -Force $ShellExtDst | Out-Null
Copy-Item $ShellExtDll -Destination $ShellExtDst -Force

# Payload.dll normally ships inside the Controller output (csproj CopyPayloadDll);
# copy it explicitly as a safety net if that step was skipped.
$PayloadDst = Join-Path $BundleDir "bin\Controller\Payload.dll"
if (-not (Test-Path $PayloadDst)) { Copy-Item $PayloadDll -Destination $PayloadDst -Force }

# ── VM-ready config (reuses installer.build_bundle_config) ───────────────────
Write-Host "Writing VM-ready config.yaml ..." -ForegroundColor Yellow
$SrcConfig  = Join-Path $RepoRoot "config.yaml"
$DestConfig = Join-Path $BundleDir "config.yaml"
& $VenvPy -c "import sys; sys.path.insert(0, r'$RepoRoot'); from orchestrator.installer import build_bundle_config; build_bundle_config(r'$SrcConfig', r'$DestConfig')"
if ($LASTEXITCODE -ne 0) { throw "build_bundle_config failed (exit=$LASTEXITCODE)" }

# ── install.cmd / uninstall.cmd / README ─────────────────────────────────────
$InstallCmd = @'
@echo off
REM DLP Agent installer. RIGHT-CLICK this file and choose "Run as administrator".
REM No Visual Studio / Python / dev tools needed — uses the bundled embed Python.
setlocal
set "HERE=%~dp0"
"%HERE%python-embed\python.exe" -m orchestrator --install --config "%HERE%config.yaml"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo Install FAILED with exit code %RC%.
  echo If it mentions elevation, re-run this file as administrator.
)
echo.
pause
exit /b %RC%
'@
Set-Content -Path (Join-Path $BundleDir "install.cmd") -Value $InstallCmd -Encoding ASCII

$UninstallCmd = @'
@echo off
REM DLP Agent uninstaller. Run as administrator.
REM Prefer the INSTALLED python: it is covered by the App Control self-protect
REM policy (C:\Program Files\DLP\*), so it launches even while an enforcement
REM policy is deployed. The bundle's embed python is NOT covered and WDAC would
REM block it under enforcement. Fall back to the bundle python only when the agent
REM isn't installed (in which case no self-protect policy is enforcing).
REM (The installed tree also ships its own uninstall.cmd at %ProgramFiles%\DLP,
REM usable when this bundle is gone.)
setlocal
set "PF=%ProgramFiles%\DLP"
if exist "%PF%\python\python.exe" (
  "%PF%\python\python.exe" -m orchestrator --uninstall --config "%PF%\config.yaml"
) else (
  "%~dp0python-embed\python.exe" -m orchestrator --uninstall --config "%~dp0config.yaml"
)
echo.
pause
'@
Set-Content -Path (Join-Path $BundleDir "uninstall.cmd") -Value $UninstallCmd -Encoding ASCII

# ── dlp-ctl.cmd (admin CLI wrapper) ──────────────────────────────────────────
# Prefer the INSTALLED python + config (the running service reads admin_pipe from
# %ProgramFiles%\DLP\config.yaml), so dlp-ctl works even after the bundle folder
# is deleted, as long as it is run from inside the bundle. Fall back to the
# bundle embed if the agent isn't installed yet. Run elevated for status/reload.
$DlpCtlCmd = @'
@echo off
REM DLP Agent admin CLI: status | reload | tail. Run elevated for status/reload.
setlocal
set "PF=%ProgramFiles%\DLP"
if exist "%PF%\python\python.exe" (
  "%PF%\python\python.exe" -m orchestrator.ctl --config "%PF%\config.yaml" %*
) else (
  "%~dp0python-embed\python.exe" -m orchestrator.ctl --config "%~dp0config.yaml" %*
)
exit /b %ERRORLEVEL%
'@
Set-Content -Path (Join-Path $BundleDir "dlp-ctl.cmd") -Value $DlpCtlCmd -Encoding ASCII

$Readme = @'
DLP Endpoint Agent — deploy bundle
==================================

No Visual Studio / Developer PowerShell / system Python is needed — install runs
on the bundled embed Python + built-in Windows tools.

PREREQUISITE (one time per machine): the .NET 10 Desktop Runtime (x64).
  The interceptor apps (Controller, Clipboard, TransferAgent) are .NET 10.
  Download "Windows Desktop Runtime 10.0.x x64" from
    https://dotnet.microsoft.com/download/dotnet/10.0
  and install it, e.g. (elevated):
    windowsdesktop-runtime-10.0.x-win-x64.exe /install /quiet /norestart
  Verify:  dotnet --list-runtimes   (expect a Microsoft.WindowsDesktop.App 10.x line)

To install (elevated):
  1. Copy this whole folder (or unzip DLP.zip) anywhere on the machine.
  2. Right-click install.cmd  ->  "Run as administrator".
     It installs into %ProgramFiles%\DLP, registers the DLPAgent service
     (start type: auto — starts at boot), the mitmproxy CA, the proxy redirect,
     and the "Transfer to USB (DLP Protected)" shell extension.
  3. The installer starts the service immediately (and it auto-starts on boot).
     Verify it is Running:
       Get-Service DLPAgent
     If it is Stopped, start it with:  Start-Service DLPAgent
     (NOTE: bare `sc start` in PowerShell is an alias for Set-Content, NOT the
      service controller — use Start-Service, or sc.exe start DLPAgent.)
     Session-aware children spawn per user session; logon/logoff add/remove them.
  4. Check the log:
       Get-Content "$env:ProgramData\DLP\logs\dlp-agent.log" -Tail 40

ADMIN CLI:
  The installer adds %ProgramFiles%\DLP to PATH, so from a NEW (elevated) shell:
    dlp-ctl status          show uptime, in-flight counts, child states
    dlp-ctl reload          reload config.yaml / policies.yaml
    dlp-ctl tail --follow   stream the structured decision log (events.jsonl)
  (Or run .\dlp-ctl.cmd status from %ProgramFiles%\DLP if PATH hasn't refreshed.)
  status / reload require an elevated prompt; per-decision audit log:
    %ProgramData%\DLP\logs\events.jsonl

APP CONTROL (WDAC):
  The agent includes an App Control channel. Install auto-enables the ConfigCI
  module (so on-endpoint policy building works) and creates the drop-folder tree
  under %ProgramData%\DLP\appcontrol; no policy is deployed until you push one.
  Author/build/apply policies with `dlp-ctl appcontrol allow|deny|build|apply`.
  Set app_control.enabled: false in config.yaml to skip the channel entirely.

To uninstall (elevated):
  Run uninstall.cmd as administrator (reverses everything; safe to re-run).
  It also removes any deployed App Control policy (no reboot) and the whole
  %ProgramData%\DLP\appcontrol tree.
  The installer also drops %ProgramFiles%\DLP\uninstall.cmd, so you can uninstall
  even after deleting this bundle. Both uninstallers run from the INSTALLED python,
  which the App Control self-protect policy allows, so uninstall works even while a
  policy is enforced.
  To RE-INSTALL over a still-enforced policy, first disable it: dlp-ctl appcontrol
  disable (then run install.cmd).

After install you may delete this bundle folder to reclaim disk space.
'@
Set-Content -Path (Join-Path $BundleDir "README-DEPLOY.txt") -Value $Readme -Encoding ASCII

# ── Zip ──────────────────────────────────────────────────────────────────────
Write-Host "Compressing to $ZipPath ..." -ForegroundColor Yellow
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Compress-Archive -Path $BundleDir -DestinationPath $ZipPath -Force

$BundleSize = "{0:N1} MB" -f ((Get-ChildItem $BundleDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
$ZipSize    = "{0:N1} MB" -f ((Get-Item $ZipPath).Length / 1MB)

Write-Host ""
Write-Host "Bundle ready." -ForegroundColor Green
Write-Host "  $BundleDir  ($BundleSize)"
Write-Host "  $ZipPath  ($ZipSize)"
Write-Host "Copy/unzip to the VM and run install.cmd as administrator."
