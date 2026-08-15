[CmdletBinding()]
param(
    [string]$DestinationRoot = "",
    [string]$Channel = "10.0",
    [string]$Version = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$scriptRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($DestinationRoot)) {
    $DestinationRoot = Join-Path $scriptRoot "runtime\dotnet"
}
$DestinationRoot = [System.IO.Path]::GetFullPath($DestinationRoot)
New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null

$dotnetInstallScript = Join-Path ([System.IO.Path]::GetTempPath()) ("dotnet-install-" + [guid]::NewGuid().ToString("N") + ".ps1")
try {
    Write-Host "Downloading the official dotnet-install.ps1 script..."
    Invoke-WebRequest `
        -Uri "https://dot.net/v1/dotnet-install.ps1" `
        -OutFile $dotnetInstallScript `
        -UseBasicParsing

    $installArguments = @{
        InstallDir = $DestinationRoot
        Architecture = "x64"
        NoPath = $true
    }
    if ([string]::IsNullOrWhiteSpace($Version)) {
        $installArguments.Channel = $Channel
    } else {
        $installArguments.Version = $Version
    }

    Write-Host "Installing .NET SDK into $DestinationRoot..."
    & $dotnetInstallScript @installArguments
    if (-not $?) {
        throw "dotnet-install.ps1 failed."
    }
} finally {
    if (Test-Path -LiteralPath $dotnetInstallScript) {
        Remove-Item -LiteralPath $dotnetInstallScript -Force -ErrorAction SilentlyContinue
    }
}

$dotnetExecutable = Join-Path $DestinationRoot "dotnet.exe"
if (-not (Test-Path -LiteralPath $dotnetExecutable)) {
    throw "The .NET installer did not produce $dotnetExecutable"
}

$env:DOTNET_ROOT = $DestinationRoot
$env:PATH = $DestinationRoot + [System.IO.Path]::PathSeparator + $env:PATH
$installedSdks = (& $dotnetExecutable --list-sdks | Out-String).Trim()
if ($installedSdks -notmatch '(?m)^\s*10\.') {
    throw "The local .NET installation does not contain a 10.x SDK. Installed SDKs: $installedSdks"
}

Write-Host "Installed .NET SDK 10 under $DestinationRoot."
