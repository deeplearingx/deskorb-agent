[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$Lnk,
    [Parameter(Mandatory = $true)] [string]$Target,
    [Parameter(Mandatory = $true)] [string]$Arguments,
    [Parameter(Mandatory = $true)] [string]$WorkingDir,
    [Parameter(Mandatory = $true)] [string]$AppId,
    [string]$Icon = ""
)

# This script is invoked only after the Python side has decided that the shortcut is
# missing or stale. Keep it self-contained and idempotent: creating/updating a .lnk is
# safe, while all task actions still go through DeskOrb's normal confirmation policy.
$ErrorActionPreference = "Stop"

$parent = Split-Path -Parent $Lnk
if ($parent) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($Lnk)
$link.TargetPath = $Target
$link.Arguments = '"' + $Arguments + '"'
$link.WorkingDirectory = $WorkingDir
$link.Description = "DeskOrb Agent"
if ($Icon) {
    $link.IconLocation = $Icon + ",0"
}
$link.Save()

# Keep the AppUserModelID in a sidecar property understood by the Python process and
# future installers. The running window is stamped directly through Win32 APIs; the
# .lnk itself remains compatible with the WScript.Shell automation interface.
Write-Output (ConvertTo-Json @{ lnk = $Lnk; app_id = $AppId } -Compress)
