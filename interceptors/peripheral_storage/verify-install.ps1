# DEPRECATED in Phase D — replaced by `python -m orchestrator --install`.
#
# The original per-user (HKCU, no-admin) install script is preserved in git
# history; if you need to recover it for emergency rollback, run:
#   git log -- $PSCommandPath
# and check out the version at the commit immediately preceding Phase D.
#
# Setting $env:DLP_ALLOW_LEGACY_INSTALL=1 still exits 1 — the file is
# intentionally a tombstone, not a fall-back.

Write-Host ""
Write-Host "*** verify-install.ps1 is DEPRECATED (Phase D). ***" -ForegroundColor Yellow
Write-Host "Use the unified installer instead:" -ForegroundColor Yellow
Write-Host ""
Write-Host "    python -m orchestrator --install         # elevated Developer PowerShell"
Write-Host "    python -m orchestrator --uninstall       # idempotent cleanup"
Write-Host ""
Write-Host "Pre-built artifacts are expected under each project's bin\Debug\ folder."
Write-Host "Run scripts\prepare-install-payload.ps1 first if you haven't built yet."
Write-Host ""

if ($env:DLP_ALLOW_LEGACY_INSTALL) {
    Write-Host "DLP_ALLOW_LEGACY_INSTALL is set but legacy code is no longer in this file." -ForegroundColor Yellow
    Write-Host "Recover from git history: git log -- $PSCommandPath" -ForegroundColor Yellow
}

exit 1
