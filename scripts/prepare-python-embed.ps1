# Phase D developer prep: produce <repo_root>\python-embed\ for the installer.
#
# The installer (python -m orchestrator --install) bundles this directory as
# %ProgramFiles%\DLP\python\. It must contain a working Python 3.13 embeddable
# with pip + the project's requirements + a sitecustomize.py that makes pywin32
# DLLs discoverable.
#
# Run ONCE per dev machine from a normal (non-elevated) PowerShell at repo root:
#   .\scripts\prepare-python-embed.ps1
#
# Re-run to refresh (clears python-embed\ first).

[CmdletBinding()]
param(
    [string]$PythonVersion = "3.13.0",
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
)

$ErrorActionPreference = 'Stop'

$EmbedDir = Join-Path $RepoRoot "python-embed"
$EmbedZip = Join-Path $env:TEMP "python-$PythonVersion-embed-amd64.zip"
$GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"
$GetPip = Join-Path $env:TEMP "get-pip.py"
$EmbedUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"

Write-Host "Phase D Python embeddable prep — version $PythonVersion" -ForegroundColor Cyan
Write-Host "  RepoRoot:  $RepoRoot"
Write-Host "  EmbedDir:  $EmbedDir"

if (Test-Path $EmbedDir) {
    Write-Host "Removing existing $EmbedDir ..."
    Remove-Item -Recurse -Force $EmbedDir
}

Write-Host "Downloading embeddable distribution ..."
Invoke-WebRequest -Uri $EmbedUrl -OutFile $EmbedZip -UseBasicParsing

Write-Host "Extracting to $EmbedDir ..."
Expand-Archive -Path $EmbedZip -DestinationPath $EmbedDir -Force

# Patch python313._pth: uncomment 'import site' so site-packages is discoverable.
# Python's embeddable distribution ships this commented out by default; that's
# what blocks pip + arbitrary site-packages until we flip it on.
$PthFile = Get-ChildItem -Path $EmbedDir -Filter "python*._pth" | Select-Object -First 1
if (-not $PthFile) {
    throw "Could not find python*._pth in $EmbedDir — embeddable layout changed?"
}
Write-Host "Patching $($PthFile.Name): enabling import site + Lib\site-packages"
$pth = Get-Content -Path $PthFile.FullName -Raw
$pth = $pth -replace '(?m)^#\s*import\s+site\s*$', 'import site'
if ($pth -notmatch '(?m)^Lib\\site-packages\s*$') {
    $pth = $pth.TrimEnd("`r","`n") + "`r`nLib\site-packages`r`n"
}
# `..` puts install_root on sys.path so SCM-launched `python.exe -m orchestrator`
# can find the orchestrator/analyzer/interceptors packages from any cwd.
# Without this, the service exits with "No module named orchestrator" before
# StartServiceCtrlDispatcher can run, and SCM times out (1053).
if ($pth -notmatch '(?m)^\.\.\s*$') {
    $pth = $pth.TrimEnd("`r","`n") + "`r`n..`r`n"
}
Set-Content -Path $PthFile.FullName -Value $pth -NoNewline -Encoding ASCII

Write-Host "Downloading get-pip.py ..."
Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPip -UseBasicParsing

Write-Host "Bootstrapping pip ..."
$PyExe = Join-Path $EmbedDir "python.exe"
& $PyExe $GetPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "get-pip.py failed (exit=$LASTEXITCODE)" }

Write-Host "Installing requirements.txt ..."
$Requirements = Join-Path $RepoRoot "requirements.txt"
& $PyExe -m pip install --no-warn-script-location -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed (exit=$LASTEXITCODE)" }

# Write sitecustomize.py: makes pywin32_system32 DLLs (pywintypes313.dll,
# pythoncom313.dll) findable by `import win32service` etc. on the embeddable
# distribution, which otherwise doesn't run pywin32's post-install hook.
$SiteCustomize = Join-Path $EmbedDir "sitecustomize.py"
$SiteCustomizeBody = @'
"""Auto-loaded by site.py at interpreter startup.
Phase D prep wrote this to make pywin32 DLLs findable in the embeddable Python.
"""
import os
import pathlib
_here = pathlib.Path(__file__).resolve().parent
_pywin32 = _here / "Lib" / "site-packages" / "pywin32_system32"
if _pywin32.is_dir():
    os.add_dll_directory(str(_pywin32))
'@
Set-Content -Path $SiteCustomize -Value $SiteCustomizeBody -Encoding UTF8

Write-Host "Validating: import win32service, win32event, servicemanager ..."
& $PyExe -c "import win32service, win32event, servicemanager, mitmproxy, yaml, watchdog; print('embed OK')"
if ($LASTEXITCODE -ne 0) { throw "pywin32/mitmproxy/yaml/watchdog import failed (exit=$LASTEXITCODE)" }

Write-Host ""
Write-Host "Done. python-embed\ is ready for `python -m orchestrator --install`." -ForegroundColor Green
