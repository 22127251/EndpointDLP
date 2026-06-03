# Phase D developer prep: build all C# + C++ artifacts the installer expects.
#
# Run from a Visual Studio 2026 Developer PowerShell at repo root:
#   .\scripts\prepare-install-payload.ps1
#
# After this script completes, `python -m orchestrator --install` (elevated)
# can read paths from config.yaml, find every artifact, and copy them into
# %ProgramFiles%\DLP\.

[CmdletBinding()]
param(
    [ValidateSet('Debug','Release')]
    [string]$Configuration = 'Debug',
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
)

$ErrorActionPreference = 'Stop'

$MSBuild = "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe"
if (-not (Test-Path $MSBuild)) {
    throw "MSBuild not found at $MSBuild. Open a VS 2026 Developer PowerShell or update the path."
}

Write-Host "Building DLP artifacts ($Configuration) under $RepoRoot" -ForegroundColor Cyan

# .NET 10 — dotnet build
$DotnetProjects = @(
    "src\ClipboardInterceptor\ClipboardInterceptor.csproj",
    "interceptors\peripheral_storage\Controller\Controller.csproj",
    "interceptors\peripheral_storage\TransferAgent\DlpTransferAgent.csproj"
)
foreach ($proj in $DotnetProjects) {
    $full = Join-Path $RepoRoot $proj
    Write-Host "dotnet build $proj" -ForegroundColor Yellow
    dotnet build $full -c $Configuration
    if ($LASTEXITCODE -ne 0) { throw "dotnet build $proj failed (exit=$LASTEXITCODE)" }
}

# C++ — msbuild. The two .vcxproj files need different invocations:
#   - Payload.vcxproj uses the default project-local OutDir, so a bare
#     msbuild call produces Payload\x64\<Config>\Payload.dll as expected.
#   - DlpShellExt.vcxproj declares <OutDir>$(SolutionDir)out\ShellExtension\
#     $(Configuration)\</OutDir>. Without /p:SolutionDir, MSBuild defaults
#     $(SolutionDir) to the .vcxproj's own directory, sending the DLL to
#     ShellExtension\out\ShellExtension\<Config>\ instead of the expected
#     interceptors\peripheral_storage\out\ShellExtension\<Config>\. The
#     legacy verify-install.ps1 set SolutionDir explicitly; we do the same.
$PayloadProj = Join-Path $RepoRoot "interceptors\peripheral_storage\Payload\Payload.vcxproj"
Write-Host "msbuild Payload" -ForegroundColor Yellow
& $MSBuild $PayloadProj "/p:Configuration=$Configuration" "/p:Platform=x64"
if ($LASTEXITCODE -ne 0) { throw "msbuild Payload failed (exit=$LASTEXITCODE)" }

# Trailing backslash is REQUIRED — MSBuild concatenates $(SolutionDir) with
# "out\..." with no separator, so a missing slash yields a malformed path.
$ShellExtSolutionDir = (Join-Path $RepoRoot "interceptors\peripheral_storage") + "\"
$ShellExtProj = Join-Path $RepoRoot "interceptors\peripheral_storage\ShellExtension\DlpShellExt.vcxproj"
Write-Host "msbuild DlpShellExt (SolutionDir=$ShellExtSolutionDir)" -ForegroundColor Yellow
& $MSBuild $ShellExtProj "/p:Configuration=$Configuration" "/p:Platform=x64" "/p:SolutionDir=$ShellExtSolutionDir"
if ($LASTEXITCODE -ne 0) { throw "msbuild DlpShellExt failed (exit=$LASTEXITCODE)" }

# Sanity check — confirm every artifact the installer's verify_artifacts step
# expects actually landed on disk. Paths mirror the defaults in config.yaml's
# paths: section.
$Expected = @(
    "src\ClipboardInterceptor\bin\$Configuration\net10.0-windows\ClipboardInterceptor.exe",
    "interceptors\peripheral_storage\Controller\bin\$Configuration\net10.0-windows\win-x64\UsbDlpController.exe",
    "interceptors\peripheral_storage\TransferAgent\bin\$Configuration\net10.0-windows\win-x64\DlpTransferAgent.exe",
    "interceptors\peripheral_storage\Payload\x64\$Configuration\Payload.dll",
    "interceptors\peripheral_storage\out\ShellExtension\$Configuration\DlpShellExt.dll"
)
$Missing = @()
foreach ($rel in $Expected) {
    $full = Join-Path $RepoRoot $rel
    if (-not (Test-Path $full)) { $Missing += $full }
}
if ($Missing.Count -gt 0) {
    Write-Host "The following artifacts were expected but not produced:" -ForegroundColor Red
    $Missing | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    throw "Artifact verification failed."
}

Write-Host ""
Write-Host "All artifacts built and verified. Ready for `python -m orchestrator --install`." -ForegroundColor Green
Write-Host "If you haven't yet, also run .\scripts\prepare-python-embed.ps1 for the bundled Python." -ForegroundColor Green
