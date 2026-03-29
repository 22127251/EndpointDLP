param(
    [ValidateSet("allow", "block")]
    [string]$Decision = "allow"
)

$base = $PSScriptRoot
$root = Join-Path $base ".."
$venvPath = Join-Path $root ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"


Write-Host "[*] Starting stub consumer with decision: $Decision" 
Write-Host "[*] Press Ctrl+C to stop`n" 


& $venvPython (Join-Path $root "stub_consumer.py") --decision $Decision
