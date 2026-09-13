[CmdletBinding()]
param(
    [string]$OutputRoot = "",
    [switch]$SkipPythonPack,
    [switch]$KeepStage,
    [switch]$SkipCompile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $root "artifacts\installer"
}
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
if ([System.IO.Path]::GetPathRoot($OutputRoot) -ine "D:\") {
    throw "Installer output must stay on D:. Resolved path: $OutputRoot"
}
$stage = Join-Path $OutputRoot "stage"
$pythonArchive = Join-Path $OutputRoot "deskorb-agent-python.zip"
$iss = Join-Path $PSScriptRoot "DeskOrb-Agent.iss"

function Require-File {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label was not found: $Path"
    }
}

function Require-Directory {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label was not found: $Path"
    }
}

function Copy-Tree {
    param(
        [string]$Source,
        [string]$Destination,
        [string[]]$ExcludedDirectories = @()
    )
    Require-Directory $Source "Source directory"
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $robocopyArguments = @(
        $Source, $Destination, "/E", "/R:1", "/W:1",
        "/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS", "/NP"
    )
    foreach ($excluded in $ExcludedDirectories) {
        $robocopyArguments += @("/XD", (Join-Path $Source $excluded))
    }
    & robocopy @robocopyArguments | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "Could not copy $Source to $Destination. robocopy exit code: $LASTEXITCODE"
    }
}

function Resolve-InnoCompiler {
    $command = Get-Command iscc.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) { return [string]$command.Source }
    foreach ($candidate in @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path $root "artifacts\inno-setup\ISCC.exe")
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
if (Test-Path -LiteralPath $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$rootFiles = Get-ChildItem -LiteralPath $root -File | Where-Object {
    $allowedExtensions = @(".py", ".cmd", ".ps1", ".yml", ".yaml", ".txt", ".md", ".json", ".ini", ".example")
    $isExample = $_.Name -eq ".env.example" -or $_.Name -eq "volcengine.env.example"
    ($allowedExtensions -contains $_.Extension.ToLowerInvariant() -or $isExample) -and
        $_.Name -notin @("AGENTS.md", "start_deskorb_agent.cmd")
}
foreach ($file in $rootFiles) {
    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $stage $file.Name) -Force
}
Require-File (Join-Path $root "LICENSE") "License"
Copy-Item -LiteralPath (Join-Path $root "LICENSE") -Destination (Join-Path $stage "LICENSE") -Force

Copy-Tree (Join-Path $root "packaging") (Join-Path $stage "packaging") @()
Copy-Tree (Join-Path $root ".playwright-mcp") (Join-Path $stage ".playwright-mcp") @(".git", ".npm-cache")
Copy-Tree (Join-Path $root "whisperX-main") (Join-Path $stage "whisperX-main") @(".git", ".venv", "outputs", ".cache", "__pycache__")
Copy-Tree (Join-Path $root "tools\officecli") (Join-Path $stage "tools\officecli") @()

$runtimeSource = Join-Path $root "runtime"
foreach ($runtimeDirectory in @("node", "pwsh", "git")) {
    Copy-Tree (Join-Path $runtimeSource $runtimeDirectory) (Join-Path $stage "runtime\$runtimeDirectory") @()
}
Require-File (Join-Path $stage "runtime\node\node.exe") "Portable Node.js runtime"
Require-File (Join-Path $stage "runtime\pwsh\pwsh.exe") "Portable PowerShell runtime"
Require-File (Join-Path $stage "runtime\git\cmd\git.exe") "Portable Git runtime"
Require-File (Join-Path $stage "tools\officecli\officecli.exe") "OfficeCLI binary"

$pythonRoot = Join-Path $stage "runtime\python"
if (-not $SkipPythonPack) {
    $condaPack = Get-Command conda-pack.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $condaPack) {
        throw "conda-pack was not found. Install it in the build environment first."
    }
    if (Test-Path -LiteralPath $pythonArchive) {
        Remove-Item -LiteralPath $pythonArchive -Force
    }
    Write-Host "Packing the deskorb-agent Python environment..."
    & $condaPack.Source -n deskorb-agent -o $pythonArchive --format zip
    if ($LASTEXITCODE -ne 0) {
        throw "conda-pack failed with exit code $LASTEXITCODE."
    }
} elseif (-not (Test-Path -LiteralPath $pythonArchive -PathType Leaf)) {
    throw "-SkipPythonPack was requested, but the cached Python archive was not found: $pythonArchive"
}
if (Test-Path -LiteralPath $pythonRoot) {
    Remove-Item -LiteralPath $pythonRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $pythonRoot | Out-Null
Expand-Archive -LiteralPath $pythonArchive -DestinationPath $pythonRoot -Force
Require-File (Join-Path $pythonRoot "python.exe") "Bundled Python runtime"
Require-File (Join-Path $pythonRoot "Scripts\conda-unpack.exe") "conda-unpack runtime helper"

Write-Host "Running bundled-runtime import smoke test..."
$smokeCode = "from PIL import Image; import keyboard, win32com.client, numpy, soundcard, opencc; print('core-imports-ok')"
& (Join-Path $pythonRoot "python.exe") -c $smokeCode
if ($LASTEXITCODE -ne 0) {
    throw "Bundled Python dependency smoke test failed."
}

Require-File (Join-Path $stage "deskorb_agent.py") "DeskOrb Agent entry point"
Require-File (Join-Path $stage ".playwright-mcp\cli.js") "Playwright MCP CLI"
Require-File (Join-Path $stage ".playwright-mcp\node_modules\playwright-core\package.json") "Playwright MCP dependencies"
$chromium = Get-ChildItem -LiteralPath (Join-Path $stage ".playwright-mcp\ms-playwright") -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "chromium-*" } | Select-Object -First 1
if ($null -eq $chromium) {
    throw "Bundled Chromium was not found in the Playwright cache."
}

if (-not $SkipCompile) {
    $compiler = Resolve-InnoCompiler
    if ([string]::IsNullOrWhiteSpace($compiler)) {
        throw "Inno Setup ISCC.exe was not found. Install Inno Setup 6 before compiling."
    }
    Write-Host "Compiling the Inno Setup installer..."
    & $compiler ("/DReleaseRoot=" + $stage) ("/DOutputDir=" + $OutputRoot) $iss
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE."
    }
    $installer = Join-Path $OutputRoot "DeskOrb-Agent-0.2.0-Setup-x64.exe"
    Require-File $installer "Installer output"
    $installerInfo = Get-Item -LiteralPath $installer
    Write-Host ("Installer created: {0} ({1} MB)" -f $installer, [math]::Round($installerInfo.Length / 1MB, 1))
}

if (-not $KeepStage -and (Test-Path -LiteralPath $stage)) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
