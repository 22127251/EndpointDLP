# AC-1 spike: assemble the VM staging payload on the dev machine.
#
# Builds <repo>\tmp\ac1-stage\ (tmp\ is gitignored) with everything the VM
# runbook needs; point the VMware shared folder at it, then copy the whole
# tree to C:\spike\ INSIDE the guest (local copy first, so a policy that
# breaks the shared-folder driver cannot strand the run).
#
# Stage layout:
#   cli\         base.xml, Add-WDACRule.ps1, add-wdacwrule.py, policies\{p1,p2,p2a,pn}\
#   scripts\     spike-evt-subscribe.py, spike-versioninfo-dump.ps1, spike-neutralize-policy.ps1
#   lists\       p1-allow.txt, p2-allow.txt, p2-deny.txt (fix FIX-ME lines on the VM)
#   python-embed\  full bundled Python (pywin32 included)
#   tools\       empty - download PsExec64.exe here inside the VM
#   artifacts\   events\, citool-json\, versioninfo\ - runbook outputs land here

[CmdletBinding()]
param(
    [string] $StageDir
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $StageDir) { $StageDir = Join-Path $repoRoot 'tmp\ac1-stage' }

$embedSrc = Join-Path $repoRoot 'python-embed'
if (-not (Test-Path (Join-Path $embedSrc 'python.exe'))) {
    throw "python-embed not found at $embedSrc - run scripts\prepare-python-embed.ps1 first."
}

$cliSrc = Join-Path $repoRoot 'interceptors\app_control\cli'
foreach ($f in 'base.xml', 'Add-WDACRule.ps1', 'add-wdacwrule.py') {
    if (-not (Test-Path (Join-Path $cliSrc $f))) { throw "missing CLI tool file: $cliSrc\$f" }
}

$dirs = @(
    'cli\policies\p1', 'cli\policies\p2', 'cli\policies\p2a', 'cli\policies\pn',
    'scripts', 'lists', 'tools',
    'artifacts\events', 'artifacts\citool-json', 'artifacts\versioninfo'
)
foreach ($d in $dirs) {
    New-Item -ItemType Directory -Path (Join-Path $StageDir $d) -Force | Out-Null
}

foreach ($f in 'base.xml', 'Add-WDACRule.ps1', 'add-wdacwrule.py') {
    Copy-Item (Join-Path $cliSrc $f) (Join-Path $StageDir 'cli') -Force
}
foreach ($f in 'spike-evt-subscribe.py', 'spike-versioninfo-dump.ps1', 'spike-neutralize-policy.ps1') {
    Copy-Item (Join-Path $PSScriptRoot $f) (Join-Path $StageDir 'scripts') -Force
}
Copy-Item (Join-Path $PSScriptRoot 'spike-lists\*.txt') (Join-Path $StageDir 'lists') -Force

Write-Host "copying python-embed (this is the slow part)..."
$null = robocopy $embedSrc (Join-Path $StageDir 'python-embed') /E /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
$Global:LASTEXITCODE = 0

Write-Host ""
Write-Host "staged AC-1 payload at: $StageDir"
Write-Host "next: point the VMware shared folder at it, copy to C:\spike\ inside the VM,"
Write-Host "      download PsExec64.exe into C:\spike\tools\, then follow the runbook."
