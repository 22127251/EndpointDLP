# AC-1 spike: dump PE version-info + Authenticode signer for candidate rule targets.
#
# WDAC FileAttrib rules key on PE version-info fields (InternalName by default);
# files with an EMPTY field would abort Add-WDACRule.ps1, so this script flags
# them loudly. The signer column answers "would the base policy's Microsoft
# signer rules already allow/deny this?" (e.g. OneDrive / VMware Tools / PsExec).
#
# Folders are expanded NON-recursively to *.exe,*.dll,*.pyd only (a recursive
# scan would pick up non-PE files, which is exactly the trap add-wdacwrule.py's
# rglob has).
#
# Usage (Windows PowerShell 5.1 compatible):
#   powershell -NoProfile -ExecutionPolicy Bypass -File spike-versioninfo-dump.ps1 `
#       -Paths "C:\Program Files\7-Zip","C:\Program Files\VMware\VMware Tools" `
#       -OutCsv C:\spike\artifacts\versioninfo\targets.csv

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]] $Paths,

    [string] $OutCsv
)

$ErrorActionPreference = 'Stop'

$files = New-Object System.Collections.Generic.List[string]
foreach ($p in $Paths) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Warning "MISSING: $p"
        continue
    }
    $item = Get-Item -LiteralPath $p
    if ($item.PSIsContainer) {
        Get-ChildItem -LiteralPath $p -File |
            Where-Object { $_.Extension -in '.exe', '.dll', '.pyd' } |
            ForEach-Object { $files.Add($_.FullName) }
    } else {
        $files.Add($item.FullName)
    }
}

$rows = foreach ($f in $files) {
    $ver = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($f)
    $sig = Get-AuthenticodeSignature -LiteralPath $f
    $signer = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '' }
    if ([string]::IsNullOrEmpty($ver.InternalName)) {
        Write-Warning "EMPTY InternalName (would abort Add-WDACRule.ps1): $f"
    }
    [pscustomobject]@{
        Path             = $f
        InternalName     = $ver.InternalName
        OriginalFilename = $ver.OriginalFilename
        FileDescription  = $ver.FileDescription
        ProductName      = $ver.ProductName
        FileVersion      = $ver.FileVersion
        CompanyName      = $ver.CompanyName
        SignatureStatus  = $sig.Status.ToString()
        SignerSubject    = $signer
    }
}

$rows | Format-Table Path, InternalName, OriginalFilename, FileVersion, SignatureStatus -AutoSize | Out-String -Width 300 | Write-Host

if ($OutCsv) {
    $outDir = Split-Path -Parent $OutCsv
    if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }
    $rows | Export-Csv -LiteralPath $OutCsv -NoTypeInformation -Encoding UTF8
    Write-Host "Wrote $($rows.Count) row(s) to $OutCsv"
}
