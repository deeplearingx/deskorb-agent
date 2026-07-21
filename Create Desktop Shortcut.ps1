$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $root "Start Codex Overlay.cmd"
$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "Codex Overlay.lnk"
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $launcher
$link.WorkingDirectory = $root
$link.Description = "Screen-aware Codex overlay"
$link.Save()
Write-Output $linkPath

