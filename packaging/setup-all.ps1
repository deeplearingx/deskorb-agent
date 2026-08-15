[CmdletBinding()]
param(
    [switch]$InstallPortableTools,
    [switch]$InstallDotNet,
    [switch]$InstallWhisperX,
    [switch]$BuildOfficeCli,
    [switch]$Strict,
    [switch]$ForcePortableTools,
    [string]$NodeVersion = "",
    [string]$PowerShellVersion = "",
    [string]$GitVersion = "",
    [string]$DotNetVersion = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$runtimeRoot = Join-Path $root "runtime"
$toolScript = Join-Path $PSScriptRoot "install-portable-tools.ps1"
$dotnetScript = Join-Path $PSScriptRoot "install-dotnet-sdk.ps1"

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$Label
    )
    Write-Host ""
    Write-Host "== $Label =="
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

if ($InstallPortableTools) {
    $portableArguments = @{
        Tools = @("Node", "PowerShell", "Git")
        DestinationRoot = $runtimeRoot
    }
    if (-not [string]::IsNullOrWhiteSpace($NodeVersion)) { $portableArguments.NodeVersion = $NodeVersion }
    if (-not [string]::IsNullOrWhiteSpace($PowerShellVersion)) { $portableArguments.PowerShellVersion = $PowerShellVersion }
    if (-not [string]::IsNullOrWhiteSpace($GitVersion)) { $portableArguments.GitVersion = $GitVersion }
    if ($ForcePortableTools) {
        & $toolScript @portableArguments -Force
    } else {
        & $toolScript @portableArguments
    }
    if (-not $?) {
        throw "Portable tool installation failed."
    }
}

if ($InstallDotNet) {
    $dotnetArguments = @{
        DestinationRoot = (Join-Path $runtimeRoot "dotnet")
    }
    if (-not [string]::IsNullOrWhiteSpace($DotNetVersion)) { $dotnetArguments.Version = $DotNetVersion }
    & $dotnetScript @dotnetArguments
    if (-not $?) {
        throw "Local .NET SDK installation failed."
    }
}

$pathEntries = New-Object System.Collections.Generic.List[string]
foreach ($candidate in @(
    (Join-Path $runtimeRoot "node"),
    (Join-Path $runtimeRoot "git\cmd"),
    (Join-Path $runtimeRoot "pwsh"),
    (Join-Path $runtimeRoot "dotnet")
)) {
    if (Test-Path -LiteralPath $candidate) { $pathEntries.Add($candidate) }
}
if ($pathEntries.Count -gt 0) {
    $pathEntries.Add($env:PATH)
    $env:PATH = ($pathEntries -join [System.IO.Path]::PathSeparator)
}
$bundledPwsh = Join-Path $runtimeRoot "pwsh\pwsh.exe"
if (Test-Path -LiteralPath $bundledPwsh) {
    $env:DESKORB_AGENT_PWSH = $bundledPwsh
}
$bundledDotNet = Join-Path $runtimeRoot "dotnet\dotnet.exe"
if (Test-Path -LiteralPath $bundledDotNet) {
    $env:DOTNET_ROOT = Join-Path $runtimeRoot "dotnet"
}
$env:PYTHONNOUSERSITE = "1"
$env:CONDA_NO_PLUGINS = "true"

Invoke-Checked `
    -FilePath $env:ComSpec `
    -ArgumentList @("/d", "/s", "/c", "`"$(Join-Path $root 'setup-conda.cmd')`"") `
    -Label "Conda Python environment and Playwright MCP"

if ($InstallWhisperX) {
    Invoke-Checked `
        -FilePath $env:ComSpec `
        -ArgumentList @("/d", "/s", "/c", "`"$(Join-Path $root 'setup-whisperx.cmd')`"") `
        -Label "WhisperX meeting environment"
}

if ($BuildOfficeCli) {
    Invoke-Checked `
        -FilePath (Join-Path $root "build_officecli.ps1") `
        -ArgumentList @() `
        -Label "OfficeCLI self-contained binary"
}

$checkArguments = @()
if ($InstallWhisperX) { $checkArguments += "-RequireWhisperX" }
if ($BuildOfficeCli) { $checkArguments += "-RequireOfficeCli" }
if ($Strict) { $checkArguments += "-Strict" }
& (Join-Path $PSScriptRoot "check-environment.ps1") @checkArguments
if ($LASTEXITCODE -ne 0) {
    throw "Environment checks failed. See the report above."
}

Write-Host ""
Write-Host "DeskOrb Agent environment setup completed."
Write-Host "Run: Start DeskOrb Agent.cmd"
