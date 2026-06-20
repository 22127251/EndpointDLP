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

function Get-FileWithRetry {
    # Robust download. python.org's Fastly CDN sometimes forcibly closes an
    # Invoke-WebRequest connection mid-transfer ("An existing connection was
    # forcibly closed by the remote host"). curl.exe (ships with Windows 10/11)
    # negotiates TLS/HTTP and retries transient resets far more reliably; we fall
    # back to Invoke-WebRequest, and finally to reusing a file you downloaded by
    # hand (browser) to the same path.
    param(
        [Parameter(Mandatory)] [string]$Uri,
        [Parameter(Mandatory)] [string]$OutFile,
        [int]$MinBytes = 1,
        [int]$Retries = 4
    )
    if ((Test-Path $OutFile) -and ((Get-Item $OutFile).Length -ge $MinBytes)) {
        Write-Host ("  Reusing existing {0} ({1:N0} bytes)" -f $OutFile, (Get-Item $OutFile).Length)
        return
    }
    if (Test-Path $OutFile) { Remove-Item $OutFile -Force }   # drop a partial

    $curl = Join-Path $env:SystemRoot "System32\curl.exe"
    if (Test-Path $curl) {
        Write-Host "  curl.exe $Uri"
        & $curl -L --fail --retry $Retries --retry-delay 3 --retry-all-errors -o $OutFile $Uri
        if ($LASTEXITCODE -eq 0 -and (Test-Path $OutFile) -and (Get-Item $OutFile).Length -ge $MinBytes) { return }
        Write-Warning "curl.exe failed (exit=$LASTEXITCODE); falling back to Invoke-WebRequest."
        if (Test-Path $OutFile) { Remove-Item $OutFile -Force }
    }

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12  # helps Windows PowerShell 5.1
    for ($i = 1; $i -le $Retries; $i++) {
        try {
            Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing
            if ((Test-Path $OutFile) -and (Get-Item $OutFile).Length -ge $MinBytes) { return }
        } catch {
            Write-Warning "Download attempt $i/$Retries failed: $($_.Exception.Message)"
        }
        if (Test-Path $OutFile) { Remove-Item $OutFile -Force }
        Start-Sleep -Seconds (2 * $i)
    }
    throw ("Failed to download $Uri after $Retries attempts. If you are behind a VPN/proxy/firewall, " +
           "download it in a browser to '$OutFile' and re-run this script (it reuses an existing file).")
}

Write-Host "Phase D Python embeddable prep - version $PythonVersion" -ForegroundColor Cyan
Write-Host "  RepoRoot:  $RepoRoot"
Write-Host "  EmbedDir:  $EmbedDir"

if (Test-Path $EmbedDir) {
    Write-Host "Removing existing $EmbedDir ..."
    Remove-Item -Recurse -Force $EmbedDir
}

Write-Host "Downloading embeddable distribution ..."
Get-FileWithRetry -Uri $EmbedUrl -OutFile $EmbedZip -MinBytes 5000000   # embed is ~10 MB

Write-Host "Extracting to $EmbedDir ..."
Expand-Archive -Path $EmbedZip -DestinationPath $EmbedDir -Force

# Patch python313._pth: uncomment 'import site' so site-packages is discoverable.
# Python's embeddable distribution ships this commented out by default; that's
# what blocks pip + arbitrary site-packages until we flip it on.
$PthFile = Get-ChildItem -Path $EmbedDir -Filter "python*._pth" | Select-Object -First 1
if (-not $PthFile) {
    throw "Could not find python*._pth in $EmbedDir - embeddable layout changed?"
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
Get-FileWithRetry -Uri $GetPipUrl -OutFile $GetPip -MinBytes 50000   # get-pip.py is ~2 MB

Write-Host "Bootstrapping pip ..."
$PyExe = Join-Path $EmbedDir "python.exe"
& $PyExe $GetPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "get-pip.py failed (exit=$LASTEXITCODE)" }

Write-Host "Installing requirements.txt ..."
$Requirements = Join-Path $RepoRoot "requirements.txt"
& $PyExe -m pip install --no-warn-script-location -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed (exit=$LASTEXITCODE)" }

# Phase E (Q-E3): the real service body imports analyzer.engine, which needs the
# analyzer's heavy deps (pyahocorasick, google-re2, PyMuPDF, python-docx, ...).
# Phase D deliberately omitted these (the placeholder service didn't import the
# analyzer). Bundle them now so `python -m orchestrator --service` can run the
# real DLP loop from the embed. pyahocorasick compiles here on the dev box (which
# has the VS C++ toolchain) and ships built into Lib\site-packages, so the target
# VM needs no compiler. Expect the embed to grow to ~200-400 MB.
# pyahocorasick has no cp313 Windows wheel and the embeddable distribution can
# NOT compile C extensions (it ships no Include\ or libs\). So a plain
# `pip install pyahocorasick` against the embed would try a source build and
# fail. Instead reuse the already-compiled extension from the dev .venv: drop
# the .pyd + its dist-info into the embed's site-packages BEFORE the analyzer
# install, so pip sees pyahocorasick already satisfied and skips the build.
# (cp313 ABI is stable across 3.13.x, so a .venv 3.13.x .pyd runs on the embed.)
$VenvSitePackages  = Join-Path $RepoRoot ".venv\Lib\site-packages"
$EmbedSitePackages = Join-Path $EmbedDir "Lib\site-packages"
$AhoPyd = Get-ChildItem -Path $VenvSitePackages -Filter "ahocorasick*.pyd" -ErrorAction SilentlyContinue | Select-Object -First 1
$AhoDistInfo = Get-ChildItem -Path $VenvSitePackages -Directory -Filter "pyahocorasick-*.dist-info" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $AhoPyd -or -not $AhoDistInfo) {
    throw ("Could not find a compiled pyahocorasick in $VenvSitePackages " +
           "(need ahocorasick*.pyd + pyahocorasick-*.dist-info). Install it into " +
           "the dev .venv first from an x64 Native Tools prompt: pip install pyahocorasick")
}
Write-Host "Seeding embed with pre-compiled pyahocorasick ($($AhoPyd.Name)) from .venv ..."
if (-not (Test-Path $EmbedSitePackages)) { New-Item -ItemType Directory -Force $EmbedSitePackages | Out-Null }
Copy-Item $AhoPyd.FullName      -Destination $EmbedSitePackages -Force
Copy-Item $AhoDistInfo.FullName -Destination $EmbedSitePackages -Recurse -Force

Write-Host "Installing analyzer\requirements.txt ..."
$AnalyzerRequirements = Join-Path $RepoRoot "analyzer\requirements.txt"
& $PyExe -m pip install --no-warn-script-location -r $AnalyzerRequirements
if ($LASTEXITCODE -ne 0) { throw "pip install -r analyzer\requirements.txt failed (exit=$LASTEXITCODE)" }

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

Write-Host "Validating: orchestrator + analyzer imports ..."
& $PyExe -c "import win32service, win32event, servicemanager, mitmproxy, yaml, watchdog, ahocorasick, re2, fitz; print('embed OK')"
if ($LASTEXITCODE -ne 0) { throw "orchestrator/analyzer dep import failed (exit=$LASTEXITCODE)" }

Write-Host ""
Write-Host "Done. python-embed\ is ready for `python -m orchestrator --install`." -ForegroundColor Green
