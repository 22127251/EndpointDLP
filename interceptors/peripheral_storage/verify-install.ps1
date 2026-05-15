# Verification install script for the peripheral storage transfer module.
# Does NOT require Administrator — all registry writes go to HKCU.
# Run from a VS 2026 Developer PowerShell so msbuild is on PATH.
#
# Registry operations use reg.exe (not PowerShell cmdlets) because:
#   - New-Item -LiteralPath is NOT supported by the registry provider
#   - The key named '*' (all-files handler) is a literal name, not a glob
#   - reg.exe treats all paths literally — no wildcard expansion ever

$ErrorActionPreference = 'Stop'

$ScriptDir         = $PSScriptRoot
$TransferAgentProj = Join-Path $ScriptDir 'TransferAgent\DlpTransferAgent.csproj'
$ShellExtProj      = Join-Path $ScriptDir 'ShellExtension\DlpShellExt.vcxproj'

# SolutionDir controls the vcxproj OutDir.  Pass peripheral_storage\ so the
# DLL lands in  peripheral_storage\out\ShellExtension\Release\DlpShellExt.dll
$SolutionDir = "$ScriptDir\"

$DllPath  = Join-Path $ScriptDir 'out\ShellExtension\Release\DlpShellExt.dll'
$AgentDir = Join-Path $ScriptDir 'TransferAgent\bin\Release\net10.0-windows\win-x64\publish'
$AgentExe = Join-Path $AgentDir 'DlpTransferAgent.exe'

# CLSID must match the value in DlpContextMenu.h
$Clsid = '{B3A1C2D4-E5F6-7890-ABCD-EF1234567890}'

function Step([string]$label, [scriptblock]$action) {
    Write-Host "`n[$label]" -ForegroundColor Yellow
    & $action
}

function RegAdd([string]$key, [string]$name, [string]$value, [string]$type = 'REG_SZ') {
    if ($name -eq '') {
        reg add $key /ve /d $value /f 2>&1 | Out-Null
    } else {
        reg add $key /v $name /d $value /t $type /f 2>&1 | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { throw "reg add failed for: $key  (exit $LASTEXITCODE)" }
}

Write-Host '=== DLP Transfer Module — Verification Install (no admin) ===' -ForegroundColor Cyan

Step '1/5  Build C# TransferAgent' {
    dotnet publish $TransferAgentProj -c Release -r win-x64 --no-self-contained /nologo
    if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed (exit $LASTEXITCODE)" }
    if (-not (Test-Path -LiteralPath $AgentExe)) { throw "Expected EXE not found: $AgentExe" }
    Write-Host "     $AgentExe" -ForegroundColor Green
}

Step '2/5  Build C++ ShellExtension' {
    msbuild $ShellExtProj /p:Configuration=Release /p:Platform=x64 `
        "/p:SolutionDir=$SolutionDir" /nologo /v:minimal
    if ($LASTEXITCODE -ne 0) { throw "msbuild failed (exit $LASTEXITCODE)" }
    if (-not (Test-Path -LiteralPath $DllPath)) { throw "Expected DLL not found: $DllPath" }
    Write-Host "     $DllPath" -ForegroundColor Green
}

Step '3/5  Register CLSID in HKCU (no admin)' {
    $clsidReg  = "HKCU\Software\Classes\CLSID\$Clsid"
    RegAdd $clsidReg '' 'DLP File Transfer'
    RegAdd "$clsidReg\InProcServer32" '' $DllPath
    RegAdd "$clsidReg\InProcServer32" 'ThreadingModel' 'Apartment'
    Write-Host "     HKCU\Software\Classes\CLSID\$Clsid registered" -ForegroundColor Green
}

Step '4/5  Register context-menu handler in HKCU (no admin)' {
    RegAdd "HKCU\Software\Classes\*\shellex\ContextMenuHandlers\DLPTransfer" '' $Clsid
    Write-Host "     HKCU\Software\Classes\*\shellex\ContextMenuHandlers\DLPTransfer set" -ForegroundColor Green
}

Step '5/5  Write agent path to HKCU (no admin)' {
    RegAdd 'HKCU\SOFTWARE\DLPAgent' 'TransferAgentPath' $AgentExe
    Write-Host "     TransferAgentPath = $AgentExe" -ForegroundColor Green
}

Write-Host "`n=== Setup complete (no admin required) ===" -ForegroundColor Cyan
Write-Host @"

Next steps:
  1. Restart Explorer so it picks up the new shell extension:
         taskkill /f /im explorer.exe  &&  explorer.exe
  2. Right-click any file in Explorer.
     The context menu should show 'Transfer to USB (DLP Protected)'.
  3. Start the orchestrator (python orchestrator\main.py from the repo root).
  4. Plug in a USB drive, right-click a file, and select the menu item.
  5. Confirm the TransferForm appears, files are analyzed, and only ALLOW'd files are copied.

To uninstall (also no admin) — paste into PowerShell:
  reg delete "HKCU\Software\Classes\CLSID\$Clsid" /f
  reg delete "HKCU\Software\Classes\*\shellex\ContextMenuHandlers\DLPTransfer" /f
  reg delete "HKCU\SOFTWARE\DLPAgent" /f
"@
