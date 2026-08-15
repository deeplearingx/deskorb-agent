[CmdletBinding()]
param(
    [ValidateSet("Node", "PowerShell", "Git")]
    [string[]]$Tools = @("Node", "PowerShell", "Git"),
    [string]$DestinationRoot = "",
    [string]$NodeVersion = "",
    [string]$PowerShellVersion = "",
    [string]$GitVersion = "",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$scriptRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($DestinationRoot)) {
    $DestinationRoot = Join-Path $scriptRoot "runtime"
}
$DestinationRoot = [System.IO.Path]::GetFullPath($DestinationRoot)
New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null

function Get-GitHubRelease {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [string]$Tag = ""
    )

    $uri = if ([string]::IsNullOrWhiteSpace($Tag)) {
        "https://api.github.com/repos/$Repository/releases/latest"
    } else {
        "https://api.github.com/repos/$Repository/releases/tags/$Tag"
    }
    return Invoke-RestMethod -Uri $uri -Headers @{
        "User-Agent" = "DeskOrb-Agent-Setup/0.2.0"
        "Accept" = "application/vnd.github+json"
    }
}

function Get-NodeArtifact {
    param([string]$Version)

    if (-not [string]::IsNullOrWhiteSpace($Version)) {
        $normalized = $Version.Trim().TrimStart("v")
        return [pscustomobject]@{
            Version = $normalized
            Name = "node-v$normalized-win-x64.zip"
            Url = "https://nodejs.org/dist/v$normalized/node-v$normalized-win-x64.zip"
        }
    }

    $releases = Invoke-RestMethod -Uri "https://nodejs.org/dist/index.json"
    $release = $releases |
        Where-Object { $_.lts -and ($_.files -contains "win-x64-zip") } |
        Select-Object -First 1
    if ($null -eq $release) {
        throw "Could not find a current Node.js LTS win-x64 archive."
    }
    $versionToken = [string]$release.version
    return [pscustomobject]@{
        Version = $versionToken.TrimStart("v")
        Name = "node-$versionToken-win-x64.zip"
        Url = "https://nodejs.org/dist/$versionToken/node-$versionToken-win-x64.zip"
    }
}

function Get-PowerShellArtifact {
    param([string]$Version)

    $tag = ""
    if (-not [string]::IsNullOrWhiteSpace($Version)) {
        $tag = "v" + $Version.Trim().TrimStart("v")
    }
    $release = Get-GitHubRelease -Repository "PowerShell/PowerShell" -Tag $tag
    $asset = $release.assets |
        Where-Object { $_.name -match '^PowerShell-[0-9]+\.[0-9]+\.[0-9]+-win-x64\.zip$' } |
        Select-Object -First 1
    if ($null -eq $asset) {
        throw "Could not find a PowerShell 7 win-x64 ZIP in the selected release."
    }
    return [pscustomobject]@{
        Version = [string]$release.tag_name
        Name = [string]$asset.name
        Url = [string]$asset.browser_download_url
    }
}

function Get-GitArtifact {
    param([string]$Version)

    $tag = ""
    if (-not [string]::IsNullOrWhiteSpace($Version)) {
        $tag = "v" + $Version.Trim().TrimStart("v")
    }
    $release = Get-GitHubRelease -Repository "git-for-windows/git" -Tag $tag
    $asset = $release.assets |
        Where-Object { $_.name -match '^MinGit-.*-64-bit\.zip$' } |
        Select-Object -First 1
    if ($null -eq $asset) {
        throw "Could not find a MinGit 64-bit ZIP in the selected release."
    }
    return [pscustomobject]@{
        Version = [string]$release.tag_name
        Name = [string]$asset.name
        Url = [string]$asset.browser_download_url
    }
}

function Install-PortableArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Marker,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)]$Artifact,
        [switch]$MarkerIsInCmdDirectory,
        [switch]$Force
    )

    $markerPath = if ($MarkerIsInCmdDirectory) {
        Join-Path $Destination (Join-Path "cmd" $Marker)
    } else {
        Join-Path $Destination $Marker
    }
    if ((-not $Force) -and (Test-Path -LiteralPath $markerPath)) {
        Write-Host "$Label already exists: $markerPath"
        return
    }

    $temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("deskorb-setup-" + [guid]::NewGuid().ToString("N"))
    $archivePath = Join-Path $temporaryRoot $Artifact.Name
    $extractRoot = Join-Path $temporaryRoot "extract"
    try {
        New-Item -ItemType Directory -Force -Path $temporaryRoot, $extractRoot | Out-Null
        Write-Host "Downloading $Label $($Artifact.Version)..."
        Invoke-WebRequest -Uri $Artifact.Url -OutFile $archivePath -UseBasicParsing
        Write-Host "Extracting $Label..."
        Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force

        $markerItem = Get-ChildItem -LiteralPath $extractRoot -Filter $Marker -File -Recurse |
            Select-Object -First 1
        if ($null -eq $markerItem) {
            throw "$Label archive did not contain $Marker."
        }

        $sourceRoot = if ($MarkerIsInCmdDirectory) {
            $markerItem.Directory.Parent.FullName
        } else {
            $markerItem.Directory.FullName
        }

        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        Get-ChildItem -LiteralPath $sourceRoot -Force | ForEach-Object {
            $target = Join-Path $Destination $_.Name
            Copy-Item -LiteralPath $_.FullName -Destination $target -Recurse -Force
        }
    } finally {
        if (Test-Path -LiteralPath $temporaryRoot) {
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    if (-not (Test-Path -LiteralPath $markerPath)) {
        throw "$Label installation did not produce $markerPath."
    }
    Write-Host "$Label installed under $Destination"
}

foreach ($tool in ($Tools | Select-Object -Unique)) {
    switch ($tool) {
        "Node" {
            $artifact = Get-NodeArtifact -Version $NodeVersion
            Install-PortableArchive `
                -Label "Node.js" `
                -Marker "node.exe" `
                -Destination (Join-Path $DestinationRoot "node") `
                -Artifact $artifact `
                -Force:$Force
        }
        "PowerShell" {
            $artifact = Get-PowerShellArtifact -Version $PowerShellVersion
            Install-PortableArchive `
                -Label "PowerShell 7" `
                -Marker "pwsh.exe" `
                -Destination (Join-Path $DestinationRoot "pwsh") `
                -Artifact $artifact `
                -Force:$Force
        }
        "Git" {
            $artifact = Get-GitArtifact -Version $GitVersion
            Install-PortableArchive `
                -Label "MinGit" `
                -Marker "git.exe" `
                -Destination (Join-Path $DestinationRoot "git") `
                -Artifact $artifact `
                -MarkerIsInCmdDirectory `
                -Force:$Force
        }
    }
}

Write-Host "Portable build tools are ready under $DestinationRoot."
