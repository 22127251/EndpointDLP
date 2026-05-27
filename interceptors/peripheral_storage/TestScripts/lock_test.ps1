param(
    [Parameter(Mandatory)][string]$AgentExe,
    [Parameter(Mandatory)][string]$UsbDest
)

# --- Setup ---
$sourceFile = "$env:TEMP\dlp_lock_source.txt"
"This is a safe test document." | Set-Content $sourceFile -Encoding UTF8

if (-not (Test-Path $UsbDest)) { New-Item -ItemType Directory -Force $UsbDest | Out-Null }

$snapshotsBefore = @(Get-ChildItem $env:TEMP -Filter "dlpsnap_*.tmp" |
                     ForEach-Object { $_.FullName })

Write-Host "Step 1: Launching TransferAgent…"
$proc = Start-Process -FilePath $AgentExe `
    -ArgumentList "--dest `"$UsbDest`" `"$sourceFile`"" `
    -PassThru -WindowStyle Normal

# --- Poll for snapshot ---
Write-Host "Step 2: Waiting for snapshot…"
$snapshotPath = $null
$deadline = (Get-Date).AddSeconds(30)

while ((Get-Date) -lt $deadline -and -not $proc.HasExited) {
    $current = @(Get-ChildItem $env:TEMP -Filter "dlpsnap_*.tmp" |
                 ForEach-Object { $_.FullName })
    $newSnaps = @($current | Where-Object { $snapshotsBefore -notcontains $_ })

    if ($newSnaps.Count -gt 0) {
        $snapshotPath = $newSnaps[0]
        Write-Host "Step 3: Snapshot found: $snapshotPath"
        break
    }
    Start-Sleep -Milliseconds 200
}

if ($snapshotPath) {
    # --- Attempt 1: Open for write via FileStream ---
    Write-Host "Step 4a: Attempting FileStream write open on locked snapshot…"
    try {
        $fs = [System.IO.File]::Open(
            $snapshotPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::ReadWrite)
        $fs.Close()
        Write-Host "FAIL: FileStream write succeeded — snapshot is NOT locked."
    }
    catch [System.IO.IOException] {
        Write-Host "PASS: FileStream write blocked: $($_.Exception.Message)"
    }

    # --- Attempt 2: AppendAllText ---
    Write-Host "Step 4b: Attempting AppendAllText on locked snapshot…"
    try {
        [System.IO.File]::AppendAllText($snapshotPath, "malicious content")
        Write-Host "FAIL: AppendAllText succeeded — snapshot is NOT locked."
    }
    catch [System.IO.IOException] {
        Write-Host "PASS: AppendAllText blocked: $($_.Exception.Message)"
    }
} else {
    Write-Host "WARNING: No snapshot detected. Agent may have exited before detection."
}

$proc.WaitForExit(60000)

# --- Cleanup ---
Remove-Item $sourceFile -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $UsbDest "dlp_lock_source.txt") -Force -ErrorAction SilentlyContinue
Write-Host "Done."