Write-Host "=================================================="  
Write-Host "  EndpointDLP - Stopping All Components" -ForegroundColor White
Write-Host "=================================================="  
Write-Host ""

Write-Host "[*] Stopping mitmproxy processes..." -ForegroundColor Yellow
Get-Process | Where-Object { $_.ProcessName -eq "mitmdump" -or $_.ProcessName -eq "mitmproxy" } | Stop-Process -Force

Write-Host "[*] Stopping QueueManager processes..." -ForegroundColor Yellow
Get-Process | Where-Object { $_.ProcessName -eq "QueueManager" } | Stop-Process -Force

Write-Host "[*] Stopping ClipboardInterceptor processes..." -ForegroundColor Yellow
Get-Process | Where-Object { $_.ProcessName -eq "ClipboardInterceptor" } | Stop-Process -Force

Write-Host ""
Write-Host "[*] Turning OFF system proxy..."  
Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" `
    -Name ProxyEnable -Value 0
netsh winhttp reset proxy | Out-Null

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host "  All components stopped!" -ForegroundColor White
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""
Write-Host "[*] Press any key to exit..." -ForegroundColor Yellow
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
