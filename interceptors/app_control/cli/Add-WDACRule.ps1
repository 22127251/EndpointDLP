<#
.SYNOPSIS
Adds WDAC Allow and/or Deny rules for one or more binaries to an existing
SiPolicy XML, matching the binary by a filename-based attribute
(InternalName by default).

.DESCRIPTION
Loads an existing WDAC base policy XML (as produced by the WDAC Wizard), reads
the chosen filename-based attribute from each binary's PE version-info block
(default: InternalName), and writes a new policy XML that allows and/or
denies those binaries. The original PolicyID and BasePolicyID are preserved
and the VersionEx field is incremented (e.g. 10.3.0.0 -> 10.3.0.1).
PolicyInfo.Id is updated to today's date.

The PE metadata is read directly via [System.Diagnostics.FileVersionInfo],
which avoids the ConfigCI cmdlet path (New-CIPolicyRule -Level FileName) -
that cmdlet returns "File does not have a SIP" errors for unsigned binaries
and prompts interactively, so it is unusable for this workflow.

Each binary produces exactly one <Allow> or <Deny> element inside <FileRules>
and one <FileRuleRef> entry inside the UMCI <SigningScenario>'s
<FileRulesRef>, the same shape the WDAC Wizard emits for "File Attribute"
rules (see base_with_winrar_filename.xml for an example output).

.EXAMPLE
PS> .\Add-WDACRule.ps1 -InputPolicy  .\base.xml `
                            -OutputPolicy .\base_with_apps.xml `
                            -AllowPaths  C:\Tools\app1.exe,C:\Tools\app2.exe

.EXAMPLE
PS> .\Add-WDACRule.ps1 -InputPolicy  .\base.xml `
                            -OutputPolicy .\base_with_one.xml `
                            -AllowPaths  C:\Tools\7z2601-x64.exe

.EXAMPLE
PS> .\Add-WDACRule.ps1 -InputPolicy  .\base.xml `
                            -OutputPolicy .\base_with_winrar.xml `
                            -AllowPaths  C:\Tools\winrar-x64-722.exe `
                            -CompileBinary

.EXAMPLE
PS> .\Add-WDACRule.ps1 -InputPolicy  .\base.xml `
                            -OutputPolicy .\base_with_deny.xml `
                            -DenyPaths   C:\Tools\bad1.exe,C:\Tools\bad2.exe

.EXAMPLE
PS> .\Add-WDACRule.ps1 -InputPolicy  .\base.xml `
                            -OutputPolicy .\base_with_both.xml `
                            -AllowPaths  C:\Tools\good.exe `
                            -DenyPaths   C:\Tools\bad.exe `
                            -CompileBinary

.NOTES
Requires PowerShell 5.1+ on Windows. The output policy can be compiled with
ConvertFrom-CIPolicy into a .cip binary for deployment.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $InputPolicy,

    [Parameter(Mandatory = $true)]
    [string] $OutputPolicy,

    [string[]] $AllowPaths,

    [ValidateSet('InternalName', 'OriginalFileName', 'FileDescription', 'ProductName')]
    [string] $FileNameLevel = 'InternalName',

    [string[]] $DenyPaths,

    [switch] $CompileBinary
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($AllowPaths -and $AllowPaths.Count -eq 1 -and $AllowPaths[0] -like '*,*') {
    $AllowPaths = $AllowPaths[0].Split(',')
}
if ($DenyPaths -and $DenyPaths.Count -eq 1 -and $DenyPaths[0] -like '*,*') {
    $DenyPaths = $DenyPaths[0].Split(',')
}

if (-not (Test-Path -LiteralPath $InputPolicy)) {
    throw "Input policy not found: $InputPolicy"
}
if ((-not $AllowPaths -or $AllowPaths.Count -eq 0) -and (-not $DenyPaths -or $DenyPaths.Count -eq 0)) {
    throw "At least one of -AllowPaths or -DenyPaths must be provided."
}
$allBinInputs = @($AllowPaths) + @($DenyPaths) | Where-Object { $_ }
foreach ($b in $allBinInputs) {
    if (-not (Test-Path -LiteralPath $b)) {
        throw "Binary not found: $b"
    }
}
$resolvedInput  = (Resolve-Path -LiteralPath $InputPolicy).Path
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputPolicy)

$allowRules = New-Object System.Collections.Generic.List[object]
$denyRules  = New-Object System.Collections.Generic.List[object]

function Add-FileAttribRule {
    param(
        [System.Collections.Generic.List[object]] $List,
        [string] $Path,
        [string] $Level
    )
    $ver = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($Path)
    $value = $ver.$Level
    if ([string]::IsNullOrEmpty($value)) {
        throw "Binary '$Path' has no $Level in its version info."
    }
    $List.Add([pscustomobject]@{
        Attribute      = $Level
        AttributeValue = $value
    })
}

$i = 0
foreach ($bin in $AllowPaths) {
    $i++
    Write-Host ("[Allow {0}/{1}] {2}" -f $i, $AllowPaths.Count, $bin)
    Add-FileAttribRule -List $allowRules -Path $bin -Level $FileNameLevel
}

if ($DenyPaths -and $DenyPaths.Count -gt 0) {
    $j = 0
    foreach ($bin in $DenyPaths) {
        $j++
        Write-Host ("[Deny  {0}/{1}] {2}" -f $j, $DenyPaths.Count, $bin)
        Add-FileAttribRule -List $denyRules -Path $bin -Level $FileNameLevel
    }
}

$doc  = New-Object System.Xml.XmlDocument
$doc.Load($resolvedInput)
$root = $doc.DocumentElement

if ($root.LocalName -ne 'SiPolicy') {
    throw "Input XML root is not <SiPolicy> (got <$($root.LocalName)>)"
}

$maxA = 0
$maxD = 0
foreach ($node in $root.SelectNodes('descendant-or-self::*[@ID]')) {
    $ruleId = $node.GetAttribute('ID')
    if ($ruleId -match '^ID_ALLOW_A_(\d+)$') {
        $n = [int]$Matches[1]
        if ($n -gt $maxA) { $maxA = $n }
    } elseif ($ruleId -match '^ID_DENY_D_(\d+)$') {
        $n = [int]$Matches[1]
        if ($n -gt $maxD) { $maxD = $n }
    }
}
$startA = $maxA + 1
$endA   = $startA + $allowRules.Count - 1
if ($allowRules.Count -gt 0) {
    $newIds = @($startA..$endA | ForEach-Object { "ID_ALLOW_A_$_" })
} else {
    $newIds = @()
}
$startD = $maxD + 1
$endD   = $startD + $denyRules.Count - 1
if ($denyRules.Count -gt 0) {
    $newDenyIds = @($startD..$endD | ForEach-Object { "ID_DENY_D_$_" })
} else {
    $newDenyIds = @()
}

Write-Host ""
if ($allowRules.Count -gt 0) {
    Write-Host ("Inserting {0} Allow rule(s) (ID_ALLOW_A_{1}..ID_ALLOW_A_{2}) using $FileNameLevel" -f $allowRules.Count, $startA, $endA)
}
if ($denyRules.Count -gt 0) {
    Write-Host ("Inserting {0} Deny rule(s)  (ID_DENY_D_{1}..ID_DENY_D_{2}) using $FileNameLevel" -f $denyRules.Count, $startD, $endD)
}

$fileRules = $root.SelectSingleNode("*[local-name()='FileRules']")
if (-not $fileRules) { throw "<FileRules> not found in input policy" }
$first = $fileRules.FirstChild

function Add-FileRuleElement {
    param(
        [System.Xml.XmlDocument] $Doc,
        [System.Xml.XmlNode]    $Parent,
        [ref]                   $FirstRef,
        [string]                $Tag,
        [string]                $NewId,
        [string]                $FriendlyName,
        [string]                $Attribute,
        [string]                $AttributeValue
    )
    $el = $Doc.CreateElement($Tag, 'urn:schemas-microsoft-com:sipolicy')
    $null = $el.SetAttribute('ID', $NewId)
    $null = $el.SetAttribute('FriendlyName', $FriendlyName)
    $null = $el.SetAttribute($Attribute, $AttributeValue)
    if ($FirstRef.Value) {
        $null = $Parent.InsertBefore($el, $FirstRef.Value)
    } else {
        $null = $Parent.AppendChild($el)
    }
}

for ($k = 0; $k -lt $allowRules.Count; $k++) {
    $row = $allowRules[$k]
    Add-FileRuleElement -Doc $doc -Parent $fileRules -FirstRef ([ref]$first) `
        -Tag 'Allow' -NewId $newIds[$k] `
        -FriendlyName "Allow files based on file attributes: $($row.AttributeValue)" `
        -Attribute $row.Attribute -AttributeValue $row.AttributeValue
}

for ($k = 0; $k -lt $denyRules.Count; $k++) {
    $row = $denyRules[$k]
    Add-FileRuleElement -Doc $doc -Parent $fileRules -FirstRef ([ref]$first) `
        -Tag 'Deny' -NewId $newDenyIds[$k] `
        -FriendlyName "Deny files based on file attributes: $($row.AttributeValue)" `
        -Attribute $row.Attribute -AttributeValue $row.AttributeValue
}

$umci = $root.SelectSingleNode("*[local-name()='SigningScenarios']/*[local-name()='SigningScenario' and @Value='12']")
if (-not $umci) { throw "UMCI <SigningScenario Value=""12""> not found in input policy" }

$productSigners = $umci.SelectSingleNode("*[local-name()='ProductSigners']")
if (-not $productSigners) {
    $productSigners = $doc.CreateElement('ProductSigners', 'urn:schemas-microsoft-com:sipolicy')
    $null = $umci.AppendChild($productSigners)
}

$fileRulesRef = $productSigners.SelectSingleNode("*[local-name()='FileRulesRef']")
if (-not $fileRulesRef) {
    $fileRulesRef = $doc.CreateElement('FileRulesRef', 'urn:schemas-microsoft-com:sipolicy')
    $null = $productSigners.AppendChild($fileRulesRef)
}
$firstRef = $fileRulesRef.FirstChild

foreach ($newId in $newIds) {
    $ref = $doc.CreateElement('FileRuleRef', 'urn:schemas-microsoft-com:sipolicy')
    $null = $ref.SetAttribute('RuleID', $newId)
    if ($firstRef) {
        $null = $fileRulesRef.InsertBefore($ref, $firstRef)
    } else {
        $null = $fileRulesRef.AppendChild($ref)
    }
}
foreach ($newId in $newDenyIds) {
    $ref = $doc.CreateElement('FileRuleRef', 'urn:schemas-microsoft-com:sipolicy')
    $null = $ref.SetAttribute('RuleID', $newId)
    if ($firstRef) {
        $null = $fileRulesRef.InsertBefore($ref, $firstRef)
    } else {
        $null = $fileRulesRef.AppendChild($ref)
    }
}

$version = $root.SelectSingleNode("*[local-name()='VersionEx']")
if (-not $version) { throw "<VersionEx> not found in input policy" }
$parts = $version.InnerText.Split('.')
if ($parts.Count -ne 4) { throw "Unexpected VersionEx format: $($version.InnerText)" }
$parts[3] = ([int]$parts[3] + 1).ToString()
$version.InnerText = ($parts -join '.')

$today  = (Get-Date).ToString('yyyy-MM-dd')
$idNode = $root.SelectSingleNode("*[local-name()='Settings']/*[local-name()='Setting' and @ValueName='Id']/*[local-name()='Value']/*[local-name()='String']")
if ($idNode) {
    $idNode.InnerText = $today
}

$outDir = Split-Path -Parent $resolvedOutput
if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
    $null = New-Item -ItemType Directory -Path $outDir -Force
}

$settings             = New-Object System.Xml.XmlWriterSettings
$settings.Indent      = $true
$settings.IndentChars = '  '
$settings.Encoding    = New-Object System.Text.UTF8Encoding($false)
$writer = [System.Xml.XmlWriter]::Create($resolvedOutput, $settings)
try {
    $doc.Save($writer)
} finally {
    $writer.Close()
}

Write-Host ""
Write-Host "Output : $resolvedOutput"
Write-Host "Version: $($version.InnerText)   PolicyInfo.Id: $today"
Write-Host ("Added  : {0} <Allow> rule(s) + {1} <Deny> rule(s) + {2} <FileRuleRef> entry/entries" -f $allowRules.Count, $denyRules.Count, ($allowRules.Count + $denyRules.Count))

if ($CompileBinary) {
    $policyIdNode = $root.SelectSingleNode("*[local-name()='PolicyID']")
    if (-not $policyIdNode) { throw "<PolicyID> not found in input policy" }
    $policyId     = $policyIdNode.InnerText.Trim()
    $binOutName   = "$policyId.cip"
    $binOutPath   = Join-Path (Split-Path -Parent $resolvedOutput) $binOutName

    Write-Host ""
    Write-Host "Compiling binary: $binOutPath"
    ConvertFrom-CIPolicy -XmlFilePath $resolvedOutput -BinaryFilePath $binOutPath | Out-Null
    Write-Host "Compiled: $binOutPath"
}
