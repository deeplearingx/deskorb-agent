[CmdletBinding()]
param(
    [switch]$RequireWhisperX,
    [switch]$RequireOfficeCli,
    [switch]$Strict,
    [string]$JsonPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$results = New-Object System.Collections.Generic.List[object]

function Add-Check {
    param(
        [string]$Name,
        [ValidateSet("OK", "WARN", "FAIL")][string]$Status,
        [string]$Detail,
        [bool]$Required
    )
    $results.Add([pscustomobject]@{
        Name = $Name
        Status = $Status
        Required = $Required
        Detail = $Detail
    })
}

function Find-Executable {
    param(
        [string]$Name,
        [string[]]$Candidates = @()
    )
    foreach ($candidate in $Candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate)) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) {
        return [string]$command.Source
    }
    return $null
}

function Get-CommandOutput {
    param([string]$Executable, [string[]]$Arguments)
    try {
        $output = & $Executable @Arguments 2>&1
        return (($output | Out-String).Trim())
    } catch {
        return $_.Exception.Message
    }
}

function Test-MajorVersion {
    param([string]$Value, [int]$Minimum)
    $match = [regex]::Match($Value, '(?<!\d)(\d+)')
    if (-not $match.Success) { return $false }
    return ([int]$match.Groups[1].Value -ge $Minimum)
}

$runtimeRoot = Join-Path $root "runtime"
$pythonRuntime = $null
$bundledPython = Join-Path $runtimeRoot "python.exe"
if (Test-Path -LiteralPath $bundledPython) {
    $pythonRuntime = [System.IO.Path]::GetFullPath($bundledPython)
}
if ($null -ne $pythonRuntime) {
    Add-Check "Python runtime" "OK" (Get-CommandOutput $pythonRuntime @("--version")) $true
} else {
    $conda = Get-Command conda -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $conda) {
        Add-Check "Conda/Python" "FAIL" "Conda was not found and runtime\python.exe is missing." $true
    } else {
        $condaExecutable = [string]$conda.Source
        $envList = Get-CommandOutput $condaExecutable @("env", "list", "--json")
        $envInfo = $null
        try {
            $envInfo = $envList | ConvertFrom-Json
        } catch {
            $envInfo = $null
        }
        $deskorbEnv = $null
        if ($null -ne $envInfo -and $null -ne $envInfo.envs) {
            $deskorbEnv = @($envInfo.envs |
                Where-Object { (Split-Path -Leaf ([string]$_).TrimEnd('\')) -eq "deskorb-agent" } |
                Select-Object -First 1)
        }
        if ($null -ne $deskorbEnv -and $deskorbEnv.Count -gt 0) {
            $condaPython = Join-Path ([string]$deskorbEnv[0]) "python.exe"
            $probe = if (Test-Path -LiteralPath $condaPython) {
                Get-CommandOutput $condaPython @("--version")
            } else {
                "python.exe was not found in $($deskorbEnv[0])."
            }
            if ($probe -match 'Python 3\.1[0-3]') {
                Add-Check "Conda/Python" "OK" ("deskorb-agent: " + $probe) $true
            } else {
                Add-Check "Conda/Python" "FAIL" ("deskorb-agent probe: " + $probe) $true
            }
        } else {
            Add-Check "Conda/Python" "FAIL" "The deskorb-agent Conda environment was not found. Run setup-conda.cmd." $true
        }
    }
}

$node = Find-Executable -Name "node.exe" -Candidates @(
    (Join-Path $runtimeRoot "node\node.exe")
)
$nodeDetail = if ($null -ne $node) { Get-CommandOutput $node @("--version") } else { "Node.js 18+ was not found." }
Add-Check "Node.js" ($(if ($null -ne $node -and (Test-MajorVersion $nodeDetail 18)) { "OK" } else { "FAIL" })) $nodeDetail $true

$npm = Find-Executable -Name "npm.cmd" -Candidates @(
    (Join-Path $runtimeRoot "node\npm.cmd")
)
Add-Check "npm" ($(if ($null -ne $npm) { "OK" } else { "FAIL" })) ($(if ($null -ne $npm) { Get-CommandOutput $npm @("--version") } else { "npm.cmd was not found beside Node.js." })) $true

$git = Find-Executable -Name "git.exe" -Candidates @(
    (Join-Path $runtimeRoot "git\cmd\git.exe")
)
Add-Check "Git" ($(if ($null -ne $git) { "OK" } else { "FAIL" })) ($(if ($null -ne $git) { Get-CommandOutput $git @("--version") } else { "Git is needed only to prepare the pinned Playwright checkout." })) $true

$pwsh = Find-Executable -Name "pwsh.exe" -Candidates @(
    (Join-Path $runtimeRoot "pwsh\pwsh.exe")
)
Add-Check "PowerShell 7" ($(if ($null -ne $pwsh) { "OK" } else { "WARN" })) ($(if ($null -ne $pwsh) { Get-CommandOutput $pwsh @("--version") } else { "Optional for startup; required for Agent shell tasks." })) $false

$dotnet = Find-Executable -Name "dotnet.exe" -Candidates @(
    (Join-Path $runtimeRoot "dotnet\dotnet.exe")
)
$dotnetDetail = if ($null -ne $dotnet) { Get-CommandOutput $dotnet @("--list-sdks") } else { "dotnet SDK was not found." }
$dotnetOk = $dotnetDetail -match '(?m)^\s*10\.'
Add-Check ".NET 10 SDK" ($(if ($dotnetOk) { "OK" } elseif ($RequireOfficeCli) { "FAIL" } else { "WARN" })) ($(if ($dotnetOk) { "A .NET 10 SDK is installed." } else { $dotnetDetail })) $RequireOfficeCli

$playwrightCli = Join-Path $root ".playwright-mcp\cli.js"
$playwrightDependency = Join-Path $root ".playwright-mcp\node_modules\playwright-core\package.json"
$chromium = Get-ChildItem -LiteralPath (Join-Path $root ".playwright-mcp\ms-playwright") -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "chromium-*" } | Select-Object -First 1
$playwrightOk = (Test-Path -LiteralPath $playwrightCli) -and
    (Test-Path -LiteralPath $playwrightDependency) -and ($null -ne $chromium)
Add-Check "Playwright MCP + Chromium" ($(if ($playwrightOk) { "OK" } else { "FAIL" })) ($(if ($playwrightOk) { "Pinned local MCP and Chromium are present." } else { "Run setup-playwright-mcp.cmd." })) $true

$officeBinary = Join-Path $root "tools\officecli\officecli.exe"
Add-Check "OfficeCLI" ($(if (Test-Path -LiteralPath $officeBinary) { "OK" } elseif ($RequireOfficeCli) { "FAIL" } else { "WARN" })) ($(if (Test-Path -LiteralPath $officeBinary) { $officeBinary } else { "Run build_officecli.ps1 when OfficeCLI support is required." })) $RequireOfficeCli

$whisperRoot = Join-Path $root "whisperX-main"
$whisperPython = Join-Path $whisperRoot ".venv\Scripts\python.exe"
$whisperOk = (Test-Path -LiteralPath (Join-Path $whisperRoot "pyproject.toml")) -and (Test-Path -LiteralPath $whisperPython)
Add-Check "WhisperX" ($(if ($whisperOk) { "OK" } elseif ($RequireWhisperX) { "FAIL" } else { "WARN" })) ($(if ($whisperOk) { $whisperPython } else { "Optional; run setup-whisperx.cmd to enable meeting recording." })) $RequireWhisperX

$powerToysCandidates = @()
if ($env:LOCALAPPDATA) { $powerToysCandidates += (Join-Path $env:LOCALAPPDATA "PowerToys\PowerToys.DSC.exe") }
if ($env:ProgramFiles) { $powerToysCandidates += (Join-Path $env:ProgramFiles "PowerToys\PowerToys.DSC.exe") }
$powerToys = $powerToysCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
Add-Check "PowerToys" ($(if ($null -ne $powerToys) { "OK" } else { "WARN" })) ($(if ($null -ne $powerToys) { $powerToys } else { "Optional; PowerToys MCP will report unavailable." })) $false

$officeCandidates = @()
foreach ($programFiles in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if (-not [string]::IsNullOrWhiteSpace($programFiles)) {
        $officeCandidates += (Join-Path $programFiles "Microsoft Office\root\Office16\WINWORD.EXE")
        $officeCandidates += (Join-Path $programFiles "Microsoft Office\root\Office16\EXCEL.EXE")
    }
}
$officeApp = $officeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
Add-Check "Microsoft Office" ($(if ($null -ne $officeApp) { "OK" } else { "WARN" })) ($(if ($null -ne $officeApp) { $officeApp } else { "Optional; active Word/Excel COM features need Office installed." })) $false

$driveRoot = [System.IO.Path]::GetPathRoot($root)
$drive = New-Object System.IO.DriveInfo($driveRoot)
if ($drive.IsReady) {
    $freeGb = [math]::Round($drive.AvailableFreeSpace / 1GB, 2)
    Add-Check "Free disk space" ($(if ($freeGb -ge 5) { "OK" } else { "WARN" })) ("$freeGb GB free on $driveRoot") $false
}

Write-Host ""
Write-Host "DeskOrb Agent environment report"
$results | Format-Table Name, Status, Required, Detail -AutoSize

if (-not [string]::IsNullOrWhiteSpace($JsonPath)) {
    $jsonFullPath = [System.IO.Path]::GetFullPath($JsonPath)
    $jsonParent = Split-Path -Parent $jsonFullPath
    if ($jsonParent) { New-Item -ItemType Directory -Force -Path $jsonParent | Out-Null }
    $results | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $jsonFullPath -Encoding UTF8
    Write-Host "Report written to $jsonFullPath"
}

$failed = @($results | Where-Object { $_.Required -and $_.Status -eq "FAIL" })
$warnings = @($results | Where-Object { $_.Status -eq "WARN" })
if ($failed.Count -gt 0 -or ($Strict -and $warnings.Count -gt 0)) {
    exit 1
}
exit 0
