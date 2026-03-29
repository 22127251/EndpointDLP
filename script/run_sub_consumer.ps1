param(
    [Parameter(Mandatory)]
    [ValidateSet("allow","block")]
    [string]$Mode
)


Write-Host "[DEBUG] Starting addon with decision: $Mode"

$venvPython = Join-Path -Path $PSScriptRoot -ChildPath ".venv\Scripts\python.exe"
& "$venvPython stub_consumer.py -m $Mode"



