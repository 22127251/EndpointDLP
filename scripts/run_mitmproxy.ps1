param(
    [string]$AddonFilePath = "addon.py"
)

$proxyServer="127.0.0.1:8080"


try {
    Write-Host "[*] Turning ON proxy..."

    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" `
        -Name ProxyEnable -Value 1

    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" `
        -Name ProxyServer -Value $proxyServer

    Write-Host "[*] Starting mitmproxy (Ctrl+C to stop)..."

    $mitmproxyProcess = Start-Process powershell `
    -ArgumentList "-NoExit", "-Command", "mitmdump -s $AddonFilePath --listen-port 8080 --set termlog_verbosity=debug" `
    -PassThru

    Wait-Process $mitmproxyProcess.Id

}
finally {
    Write-Host "[*] Turning OFF proxy..."

    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" `
        -Name ProxyEnable -Value 0

    netsh winhttp reset proxy | Out-Null

    Write-Host "[*] Cleanup done!"
}