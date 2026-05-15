$base = $PSScriptRoot
$root = Join-Path $base ".."

# orchestrator

# python -m orchestrator --foreground
Start-Process powershell -WorkingDirectory $root -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", "python -m orchestrator --foreground"
)
Start-Sleep -Seconds 1

# Run mitmproxy in a separate window so it can be stopped independently (and so we can see its output/debug logs).
Start-Process powershell -WorkingDirectory $root -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $base "run_mitmproxy.ps1"),
    "-AddonFilePath", (Join-Path $root "interceptors\browser\addon.py")
)
Start-Sleep -Seconds 1
