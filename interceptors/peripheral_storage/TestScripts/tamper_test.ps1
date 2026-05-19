param(
    [Parameter(Mandatory)][string]$AgentExe,   # e.g. D:\...\DlpTransferAgent.exe
    [Parameter(Mandatory)][string]$UsbDest     # e.g. E:\DLPTest
)

# --- Setup ---
$sourceFile = "$env:TEMP\dlp_tamper_source.txt"
"This is a safe test document with no sensitive data." |
    Set-Content $sourceFile -Encoding UTF8

$origHash = (Get-FileHash $sourceFile -Algorithm SHA256).Hash
Write-Host "Step 1: Source created. Hash: $origHash"

if (-not (Test-Path $UsbDest)) { New-Item -ItemType Directory -Force $UsbDest | Out-Null }

# Record existing snapshots before launch (to detect only new ones)
$snapshotsBefore = @(Get-ChildItem $env:TEMP -Filter "dlpsnap_*.tmp" |
                     ForEach-Object { $_.FullName })

# --- Launch agent ---
Write-Host "Step 2: Launching TransferAgent…"
$proc = Start-Process -FilePath $AgentExe `
    -ArgumentList "--dest `"$UsbDest`" `"$sourceFile`"" `
    -PassThru -WindowStyle Normal

# --- Poll for snapshot creation ---
Write-Host "Step 3: Waiting for snapshot in $env:TEMP …"
$snapshotFound = $false
$deadline = (Get-Date).AddSeconds(30)

while ((Get-Date) -lt $deadline -and -not $proc.HasExited) {
    $current = @(Get-ChildItem $env:TEMP -Filter "dlpsnap_*.tmp" |
                 ForEach-Object { $_.FullName })
    $newSnaps = @($current | Where-Object { $snapshotsBefore -notcontains $_ })

    if ($newSnaps.Count -gt 0) {
        Write-Host "Step 4: Snapshot detected: $($newSnaps[0])"
        Write-Host "        Modifying source file to add CCCD PII…"
        $modified = $false
        $modDeadline = (Get-Date).AddSeconds(5)
        while (-not $modified -and (Get-Date) -lt $modDeadline -and -not $proc.HasExited) {
            try {
                Add-Content -Path $sourceFile -Value "`nCCCD: 072204000310" -Encoding UTF8 -ErrorAction Stop
                $modified = $true
            } catch [System.IO.IOException] {
                Start-Sleep -Milliseconds 50
            }
        }
        if ($modified) {
            $modifiedHash = (Get-FileHash $sourceFile -Algorithm SHA256).Hash
            Write-Host "        Source file modified. New hash: $modifiedHash"
        } elseif ($proc.HasExited) {
            Write-Host "        WARNING: Agent exited before source could be modified — timing window missed."
        } else {
            Write-Host "        WARNING: Source could not be modified within 5 s — test INCONCLUSIVE."
        }
        $snapshotFound = $true
        break
    }
    Start-Sleep -Milliseconds 200
}

if (-not $snapshotFound) {
    Write-Host "WARNING: No snapshot detected within 30 s. Agent may have finished too quickly."
}

# --- Wait for agent to exit ---
Write-Host "Step 5: Waiting for TransferAgent to finish…"
$proc.WaitForExit(60000)

# --- Verify ---
$usbFile = Join-Path $UsbDest "dlp_tamper_source.txt"
Write-Host ""
if (-not $snapshotFound) {
    Write-Host "INCONCLUSIVE: No snapshot was detected within 30 s — agent may have finished before polling."
} elseif (-not $modified) {
    Write-Host "INCONCLUSIVE: Snapshot detected but source was not modified during the analysis window — security property not exercised."
} elseif (Test-Path $usbFile) {
    $usbHash = (Get-FileHash $usbFile -Algorithm SHA256).Hash
    Write-Host "USB file hash:            $usbHash"
    Write-Host "Original (pre-mod) hash:  $origHash"
    Write-Host "Modified source hash:     $((Get-FileHash $sourceFile -Algorithm SHA256).Hash)"
    Write-Host ""
    if ($usbHash -eq $origHash) {
        Write-Host "PASS: USB received pre-modification content (snapshot was used)."
    } else {
        Write-Host "FAIL: USB file does not match pre-modification snapshot."
        if ($usbHash -eq (Get-FileHash $sourceFile -Algorithm SHA256).Hash) {
            Write-Host "      USB received the MODIFIED original — vulnerability still present."
        }
    }
} else {
    Write-Host "File not found on USB. Possible causes:"
    Write-Host "  - Orchestrator blocked it (policy hit on modified original? — check orchestrator logs)"
    Write-Host "  - Analysis timed out"
    Write-Host "  - Destination exists already (run cleanup first)"
}

# --- Cleanup ---
Remove-Item $sourceFile -Force -ErrorAction SilentlyContinue
Remove-Item $usbFile    -Force -ErrorAction SilentlyContinue
Write-Host "Cleanup complete."