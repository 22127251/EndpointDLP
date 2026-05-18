$base = $PSScriptRoot
$root = Join-Path $base ".."

python -m orchestrator --foreground
