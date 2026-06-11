# AC-1 spike: build the "neutralizer" / audit-flip variants of a WDAC policy.
#
# A deployed enforcement policy is defanged WITHOUT reboot by pushing an update
# that keeps the SAME PolicyID but a HIGHER VersionEx (the deployed base policy
# has 'Enabled:Update Policy No Reboot', so `citool --refresh` applies it live):
#
#   -Mode AllowAll  : the neutralizer proper. Takes Windows' inbox example
#       policy %windir%\schemas\CodeIntegrity\ExamplePolicies\AllowAll.xml
#       (allow-everything; already carries 'Enabled:Unsigned System Integrity
#       Policy' + 'Enabled:Update Policy No Reboot') and stamps the target
#       policy's PolicyID/BasePolicyID + the requested VersionEx onto it.
#       After refresh nothing is blocked; deleting the .cip from
#       CIPolicies\Active\ then makes the policy vanish at next boot.
#
#   -Mode AuditFlip : inserts 'Enabled:Audit Mode' into a COPY of the target
#       policy (rules kept) and bumps VersionEx. Blocks become audit events
#       (3076 / 8039) instead of real blocks - used once in the spike to
#       capture audit-event samples for the AC-3 forwarder.
#
# Usage (Windows PowerShell 5.1 compatible):
#   powershell -NoProfile -ExecutionPolicy Bypass -File spike-neutralize-policy.ps1 `
#       -TargetPolicyXml base.xml -OutputPolicy policies\pn\pn.xml `
#       -VersionEx 10.3.0.9 -Mode AllowAll -CompileBinary

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $TargetPolicyXml,

    [Parameter(Mandatory = $true)]
    [string] $OutputPolicy,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+\.\d+$')]
    [string] $VersionEx,

    [ValidateSet('AllowAll', 'AuditFlip')]
    [string] $Mode = 'AllowAll',

    [switch] $CompileBinary
)

$ErrorActionPreference = 'Stop'
$SiPolicyNs = 'urn:schemas-microsoft-com:sipolicy'
$AllowAllTemplate = Join-Path $env:windir 'schemas\CodeIntegrity\ExamplePolicies\AllowAll.xml'

function Get-Node {
    param([System.Xml.XmlElement] $Root, [string] $LocalName)
    $node = $Root.SelectSingleNode("*[local-name()='$LocalName']")
    if (-not $node) { throw "Policy XML has no <$LocalName> element." }
    return $node
}

function Confirm-Option {
    # Idempotently ensure <Rules> contains <Rule><Option>$Option</Option></Rule>.
    param([System.Xml.XmlDocument] $Doc, [string] $Option)
    $root = $Doc.DocumentElement
    $rules = Get-Node -Root $root -LocalName 'Rules'
    foreach ($opt in $rules.SelectNodes(".//*[local-name()='Option']")) {
        if ($opt.InnerText.Trim() -eq $Option) {
            Write-Host "option already present: $Option"
            return
        }
    }
    $rule = $Doc.CreateElement('Rule', $SiPolicyNs)
    $opt = $Doc.CreateElement('Option', $SiPolicyNs)
    $opt.InnerText = $Option
    $rule.AppendChild($opt) | Out-Null
    $rules.AppendChild($rule) | Out-Null
    Write-Host "option inserted: $Option"
}

$resolvedTarget = (Resolve-Path -LiteralPath $TargetPolicyXml).Path

# PolicyID/BasePolicyID always come from the target policy.
$targetDoc = New-Object System.Xml.XmlDocument
$targetDoc.Load($resolvedTarget)
if ($targetDoc.DocumentElement.LocalName -ne 'SiPolicy') {
    throw "Target XML root is not <SiPolicy>."
}
$policyId = (Get-Node -Root $targetDoc.DocumentElement -LocalName 'PolicyID').InnerText.Trim()
$basePolicyId = (Get-Node -Root $targetDoc.DocumentElement -LocalName 'BasePolicyID').InnerText.Trim()

if ($Mode -eq 'AllowAll') {
    if (-not (Test-Path -LiteralPath $AllowAllTemplate)) {
        throw "Inbox template not found: $AllowAllTemplate"
    }
    $doc = New-Object System.Xml.XmlDocument
    $doc.Load($AllowAllTemplate)
    (Get-Node -Root $doc.DocumentElement -LocalName 'PolicyID').InnerText = $policyId
    (Get-Node -Root $doc.DocumentElement -LocalName 'BasePolicyID').InnerText = $basePolicyId
    Confirm-Option -Doc $doc -Option 'Enabled:Unsigned System Integrity Policy'
    Confirm-Option -Doc $doc -Option 'Enabled:Update Policy No Reboot'
} else {
    $doc = $targetDoc
    Confirm-Option -Doc $doc -Option 'Enabled:Audit Mode'
}

(Get-Node -Root $doc.DocumentElement -LocalName 'VersionEx').InnerText = $VersionEx

$outDir = Split-Path -Parent $OutputPolicy
if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}
$doc.Save($OutputPolicy)
$resolvedOutput = (Resolve-Path -LiteralPath $OutputPolicy).Path
Write-Host "wrote $Mode policy: $resolvedOutput (PolicyID=$policyId VersionEx=$VersionEx)"

if ($CompileBinary) {
    if (-not (Get-Command ConvertFrom-CIPolicy -ErrorAction SilentlyContinue)) {
        throw ("ConvertFrom-CIPolicy not available. Enable the ConfigCI module first: " +
               'gci $Env:SystemRoot\servicing\Packages\*ConfigCI*.mum | ' +
               '% { dism /online /norestart /add-package:"$($_.FullName)" }')
    }
    $cipPath = Join-Path (Split-Path -Parent $resolvedOutput) "$policyId.cip"
    ConvertFrom-CIPolicy -XmlFilePath $resolvedOutput -BinaryFilePath $cipPath | Out-Null
    Write-Host "compiled: $cipPath"
}
