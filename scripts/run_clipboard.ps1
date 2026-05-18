$base = $PSScriptRoot
$root = Join-Path $base ".."

$clipboardExe = Join-Path $root "src\ClipboardInterceptor\bin\Debug\net10.0-windows\ClipboardInterceptor.exe"
if (-not (Test-Path $clipboardExe)) {
    Write-Host "[clipboard] ClipboardInterceptor.exe not found — building..."
    dotnet build (Join-Path $root "src\ClipboardInterceptor\ClipboardInterceptor.csproj") --configuration Debug
}

& $clipboardExe
