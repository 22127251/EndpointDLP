$base = $PSScriptRoot

Write-Host "[DEBUG] Starting Clipboard Interceptor (.NET)..." 
Write-Host "[DEBUG] This will monitor clipboard and send chunks to QueueManager" 
Write-Host "[DEBUG] Press Ctrl+C to stop`n" 

dotnet run --project (Join-Path $base "..\src\ClipboardInterceptor")
