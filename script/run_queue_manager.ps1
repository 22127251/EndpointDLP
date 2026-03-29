$base = $PSScriptRoot

Write-Host "[DEBUG] Starting QueueManager (C# Core)..."  
Write-Host "[DEBUG] This will start the named pipe server on \\.\pipe\dlp_upload"
Write-Host "[DEBUG] Press Ctrl+C to stop`n" 

dotnet run --project (Join-Path $base "..\src\QueueManager")
