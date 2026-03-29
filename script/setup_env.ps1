Write-Host "[DEBUG] Setting up python environment..."
$venvPath = ".\.venv"
if (!(Test-Path $venvPath)) {
   Write-Host "Creating venv..."
   python -m venv $venvPath
}

Write-Host "[DEBUG] Installing dependencies..."
$venvPython = "$venvPath\Scripts\python.exe"
& $venvPython -m pip install -r requirements.txt

Write-Host "[DEBUG] Setup python environment done!"

Write-Host "----------------------------------------"

Write-Host "[DEBUG] Setting up dotnet environment..."
dotnet restore src/EndpointDLP.slnx
Write-Host "[DEBUG] Setup dotnet environment done!"