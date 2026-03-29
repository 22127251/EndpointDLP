$base = $PSScriptRoot
$root = Join-Path $base ".."

# 1. Start QueueManager
Write-Host "[DEBUG] Starting QueueManager..."  
Start-Process powershell -WorkingDirectory $base -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $base "run_queue_manager.ps1")
)
Start-Sleep -Seconds 1

# # 2. Start ClipboardInterceptor
Write-Host "[DEBUG] Starting Clipboard Interceptor..."  
Start-Process powershell -WorkingDirectory $base -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $base "run_clipboard.ps1")
)
Start-Sleep -Seconds 1

# # 3. Start mitmproxy
# Write-Host "[DEBUG] Starting mitmproxy..."  
# Start-Process powershell -WorkingDirectory $root -ArgumentList @(
#     "-NoExit",
#     "-ExecutionPolicy", "Bypass",
#     "-File", (Join-Path $base "run_mitmproxy.ps1")
# )

# # 4. stub_consumer
# Write-Host "[DEBUG] Starting stub consumer (allow)..."
# Start-Process powershell -WorkingDirectory $base -ArgumentList @(
#     "-NoExit",
#     "-ExecutionPolicy", "Bypass",
#     "-File", (Join-Path $base "run_sub_consumer.ps1"),
#     "-Mode", "allow"
# )
