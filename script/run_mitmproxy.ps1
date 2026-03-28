param(
    [Parameter(Mandatory)]
    [ValidateSet("allow","block")]
    [string]$Mode
)

try {

    Write-Host "[*] Setting up python environment..."
    $venvPath = ".\.venv"

    if (!(Test-Path $venvPath)) {
    Write-Host "Creating venv..."
    python -m venv $venvPath
    }

    Write-Host "[*] Installing dependencies..."

    $venvPython = "$venvPath\Scripts\python.exe"
    & $venvPython -m pip install -r requirements.txt

    Write-Host "[*] Turning ON proxy..."

    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" `
        -Name ProxyEnable -Value 1

    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" `
        -Name ProxyServer -Value "127.0.0.1:8080"

    Write-Host "[*] Starting mitmproxy (Ctrl+C to stop)..."

    $mitmproxyProcess = Start-Process powershell `
    -ArgumentList "-NoExit", "-Command", "mitmdump -s addon.py --listen-port 8080 --set termlog_verbosity=debug" `
    -PassThru

    Start-Process powershell `
    -ArgumentList "-NoExit", "-Command", "$venvPython stub_consumer.py --decision $Mode"

    # ch? ch? mitm
    Wait-Process $mitmproxyProcess.Id

}
finally {
    Write-Host "[*] Turning OFF proxy..."

    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" `
        -Name ProxyEnable -Value 0

    netsh winhttp reset proxy | Out-Null

    Write-Host "[*] Cleanup done!"
}