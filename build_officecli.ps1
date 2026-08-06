$ErrorActionPreference = "Stop"

$project = Join-Path $PSScriptRoot "OfficeCLI-main\src\officecli\officecli.csproj"
$output = Join-Path $PSScriptRoot "tools\officecli"

if (-not (Test-Path -LiteralPath $project)) {
    throw "OfficeCLI project was not found at $project"
}

$sdk10 = dotnet --list-sdks | Select-String '^\s*10\.'
if (-not $sdk10) {
    throw "OfficeCLI requires the .NET 10 SDK."
}

New-Item -ItemType Directory -Force -Path $output | Out-Null

dotnet publish $project `
    -c Release `
    -r win-x64 `
    --self-contained true `
    -p:PublishSingleFile=true `
    -p:PublishTrimmed=true `
    -o $output `
    --nologo

$binary = Join-Path $output "officecli.exe"
if (-not (Test-Path -LiteralPath $binary)) {
    throw "OfficeCLI publish did not produce $binary"
}

Write-Output "Built $binary"
