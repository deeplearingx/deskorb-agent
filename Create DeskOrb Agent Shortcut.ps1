$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $root "Start DeskOrb Agent.cmd"
$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "DeskOrb Agent.lnk"
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $launcher
$link.WorkingDirectory = $root
$link.Description = "Screen-aware DeskOrb Agent"
$link.Save()
Write-Output $linkPath
