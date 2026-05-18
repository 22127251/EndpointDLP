$base = $PSScriptRoot
$root = Join-Path $base ".."

# orchestrator
Start-Process powershell -WorkingDirectory $root -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $base "run_orchestrator.ps1")
)
Start-Sleep -Seconds 1

# mitmproxy
Start-Process powershell -WorkingDirectory $root -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $base "run_mitmproxy.ps1"),
    "-AddonFilePath", (Join-Path $root "interceptors\browser\addon.py")
)
Start-Sleep -Seconds 1

# clipboard interceptor
Start-Process powershell -WorkingDirectory $root -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $base "run_clipboard.ps1")
)
