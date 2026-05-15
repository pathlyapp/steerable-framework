<#
.SYNOPSIS
  Signtool every exe/dll/pyd inside a portable Python runtime directory.

.DESCRIPTION
  Reads the EV certificate from PFX_PATH (env), signs each binary with the
  configured timestamp server, then verifies the signatures.

.PARAMETER RuntimeRoot
  Root directory containing the portable Python runtime to sign.
#>
param(
  [Parameter(Mandatory = $true)]
  [string]$RuntimeRoot
)

$ErrorActionPreference = 'Stop'

$pfxPath = $env:WINDOWS_PFX_PATH
$pfxPassword = $env:WINDOWS_PFX_PASSWORD
if (-not $pfxPath) {
  throw "WINDOWS_PFX_PATH env required"
}
if (-not $pfxPassword) {
  throw "WINDOWS_PFX_PASSWORD env required"
}

$timestampUrl = $env:WINDOWS_TIMESTAMP_URL
if (-not $timestampUrl) {
  $timestampUrl = "http://timestamp.digicert.com"
}

$signTool = & "$env:WINDOWS_SDK_BIN\signtool.exe"
if (-not $signTool) {
  $signTool = "signtool.exe"
}

$targets = Get-ChildItem -Path $RuntimeRoot -Recurse -Include *.exe, *.dll, *.pyd
Write-Host "[signtool] discovered $($targets.Count) PE binaries"

foreach ($target in $targets) {
  Write-Host "  + $($target.FullName)"
  & $signTool sign `
    /f $pfxPath /p $pfxPassword `
    /tr $timestampUrl /td sha256 /fd sha256 `
    $target.FullName | Out-Null
}

Write-Host "[signtool] verifying signatures"
foreach ($target in $targets) {
  & $signTool verify /pa $target.FullName | Out-Null
}
