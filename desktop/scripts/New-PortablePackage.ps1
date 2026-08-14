[CmdletBinding()]
param(
    [string] $OutputDirectory,
    [string] $Version,
    [switch] $SkipDependencyInstall
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)] [string] $Description,
        [Parameter(Mandatory = $true)] [scriptblock] $Action
    )

    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Copy-DirectoryContents {
    param(
        [Parameter(Mandatory = $true)] [string] $Source,
        [Parameter(Mandatory = $true)] [string] $Destination
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Directory does not exist: $Source"
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Copy-Item -Path (Join-Path $Source "*") -Destination $Destination -Recurse -Force
}

function Write-ReleaseManifest {
    param(
        [Parameter(Mandatory = $true)] [string] $PackageRoot,
        [Parameter(Mandatory = $true)] [string] $PackageVersion
    )

    $files = @(
        Get-ChildItem -LiteralPath $PackageRoot -Recurse -File |
            Sort-Object -Property FullName |
            ForEach-Object {
                $relativePath = $_.FullName.Substring($PackageRoot.Length).TrimStart([char[]]@('\', '/')).Replace('\', '/')
                [ordered]@{
                    path = $relativePath
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                    bytes = $_.Length
                }
            }
    )
    $manifest = [ordered]@{
        schemaVersion = 1
        product = "OpenKB"
        version = $PackageVersion
        platform = "windows-x64"
        entryPoint = "OpenKB.exe"
        generatedAtUtc = [DateTime]::UtcNow.ToString("o")
        files = $files
    }
    $manifestPath = Join-Path $PackageRoot "release-manifest.json"
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $manifestPath,
        ($manifest | ConvertTo-Json -Depth 5),
        $utf8WithoutBom
    )
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$desktopRoot = Join-Path $repoRoot "desktop"
$srcTauri = Join-Path $desktopRoot "src-tauri"
$frontendRoot = Join-Path $repoRoot "frontend"
$buildRoot = Join-Path $desktopRoot ".build"
$baseTauriConfig = Join-Path $srcTauri "tauri.conf.json"
$tauriConfig = Join-Path $srcTauri "tauri.portable.conf.json"

if (-not $Version) {
    $Version = (Get-Content -Raw -LiteralPath $baseTauriConfig | ConvertFrom-Json).version
}
if (-not $Version) {
    throw "A portable package version is required."
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $desktopRoot "release"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

New-Item -ItemType Directory -Force -Path $buildRoot, $OutputDirectory | Out-Null

& (Join-Path $PSScriptRoot "Prepare-WebView2FixedRuntime.ps1")

Push-Location $repoRoot
try {
    if (-not $SkipDependencyInstall) {
        Invoke-Checked "uv dependency sync" { & uv sync --extra desktop-build --locked }
        Invoke-Checked "frontend dependency install" { & npm.cmd --prefix $frontendRoot ci }
    }
}
finally {
    Pop-Location
}

$tauriCli = Join-Path $frontendRoot "node_modules\.bin\tauri.cmd"
if (-not (Test-Path -LiteralPath $tauriCli)) {
    throw "Tauri CLI is not installed. Run without -SkipDependencyInstall or install frontend dependencies."
}

$engineRoot = Join-Path $buildRoot "engine"
$engineDist = Join-Path $engineRoot "dist"
$engineWork = Join-Path $engineRoot "work"
$engineSpec = Join-Path $engineRoot "spec"
if (Test-Path -LiteralPath $engineRoot) {
    Remove-Item -LiteralPath $engineRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $engineDist, $engineWork, $engineSpec | Out-Null

Push-Location $repoRoot
try {
    Invoke-Checked "Python Engine freeze" {
        & uv run --extra desktop-build pyinstaller `
            --noconfirm `
            --clean `
            --onedir `
            --name OpenKBEngine `
            --distpath $engineDist `
            --workpath $engineWork `
            --specpath $engineSpec `
            --paths $repoRoot `
            --collect-data openkb `
            (Join-Path $repoRoot "openkb\desktop_engine.py")
    }
}
finally {
    Pop-Location
}

Push-Location $srcTauri
try {
    Invoke-Checked "Tauri Shell build" { & $tauriCli build --config $tauriConfig --no-bundle }
}
finally {
    Pop-Location
}

$shellExe = Join-Path $srcTauri "target\release\OpenKB.exe"
$engineDirectory = Join-Path $engineDist "OpenKBEngine"
$engineExe = Join-Path $engineDirectory "OpenKBEngine.exe"
$webViewRuntime = Join-Path $srcTauri "runtime\webview2"
foreach ($requiredPath in @($shellExe, $engineExe, (Join-Path $webViewRuntime "msedgewebview2.exe"))) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Portable package prerequisite is missing: $requiredPath"
    }
}

$packageName = "OpenKB-$Version-windows-x64"
$packageRoot = Join-Path $buildRoot $packageName
if (Test-Path -LiteralPath $packageRoot) {
    Remove-Item -LiteralPath $packageRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null

Copy-Item -LiteralPath $shellExe -Destination (Join-Path $packageRoot "OpenKB.exe") -Force
Copy-DirectoryContents -Source $engineDirectory -Destination (Join-Path $packageRoot "runtime\engine")
Copy-DirectoryContents -Source $webViewRuntime -Destination (Join-Path $packageRoot "runtime\webview2")
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination $packageRoot -Force
Copy-Item -LiteralPath (Join-Path $desktopRoot "THIRD_PARTY_NOTICES.md") -Destination $packageRoot -Force
Write-ReleaseManifest -PackageRoot $packageRoot -PackageVersion $Version

$zipPath = Join-Path $OutputDirectory "$packageName.zip"
$checksumPath = "$zipPath.sha256"
foreach ($existingOutput in @($zipPath, $checksumPath)) {
    if (Test-Path -LiteralPath $existingOutput) {
        Remove-Item -LiteralPath $existingOutput -Force
    }
}
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $packageRoot,
    $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $true
)
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText(
    $checksumPath,
    "$zipHash *$([System.IO.Path]::GetFileName($zipPath))`n",
    (New-Object System.Text.UTF8Encoding($false))
)

Write-Host "Portable package created: $zipPath"
Write-Host "SHA-256 file created: $checksumPath"
