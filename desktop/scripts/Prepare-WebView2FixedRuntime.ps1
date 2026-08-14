[CmdletBinding()]
param(
    [string] $ArchivePath,
    [string] $Destination
)

$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$metadataPath = Join-Path $repoRoot "desktop\webview2-fixed-runtime.json"
$metadata = Get-Content -Raw $metadataPath | ConvertFrom-Json

foreach ($property in @("schemaVersion", "version", "architecture", "archiveFileName", "archiveUrl", "sha256")) {
    if (-not ($metadata.PSObject.Properties.Name -contains $property)) {
        throw "Fixed WebView2 metadata is missing $property."
    }
}
if ($metadata.schemaVersion -ne 1 -or $metadata.architecture -ne "x64") {
    throw "Fixed WebView2 metadata has an unsupported schema or architecture."
}
if ($metadata.version -isnot [string] -or $metadata.version -notmatch "^\d+\.\d+\.\d+\.\d+$") {
    throw "Fixed WebView2 metadata has an invalid version."
}
if ($metadata.archiveFileName -isnot [string] -or
    [System.IO.Path]::GetFileName($metadata.archiveFileName) -ne $metadata.archiveFileName -or
    $metadata.archiveFileName -notmatch "\.cab$") {
    throw "Fixed WebView2 metadata has an invalid archive filename."
}
$archiveUri = $null
if ($metadata.archiveUrl -isnot [string] -or
    -not [Uri]::TryCreate($metadata.archiveUrl, [UriKind]::Absolute, [ref] $archiveUri) -or
    $archiveUri.Scheme -ne "https") {
    throw "Fixed WebView2 metadata must contain an HTTPS archive URL."
}
if ($metadata.sha256 -isnot [string] -or $metadata.sha256 -notmatch "^[0-9a-fA-F]{64}$") {
    throw "Fixed WebView2 metadata has an invalid SHA-256 value."
}

if (-not $Destination) {
    $Destination = Join-Path $repoRoot "desktop\src-tauri\runtime\webview2"
}
if (-not $ArchivePath) {
    $cacheRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path $repoRoot "desktop\.build" }
    New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null
    $ArchivePath = Join-Path $cacheRoot $metadata.archiveFileName
}

if (-not (Test-Path -LiteralPath $ArchivePath)) {
    Write-Host "Downloading fixed WebView2 runtime $($metadata.version)"
    Invoke-WebRequest -Uri $metadata.archiveUrl -OutFile $ArchivePath
}

$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ArchivePath).Hash.ToLowerInvariant()
if ($actualHash -ne $metadata.sha256.ToLowerInvariant()) {
    throw "Fixed WebView2 archive hash mismatch. Expected $($metadata.sha256), got $actualHash."
}

$expandedRoot = Join-Path ([System.IO.Path]::GetDirectoryName($Destination)) ".webview2-expanded"
if (Test-Path -LiteralPath $expandedRoot) {
    Remove-Item -LiteralPath $expandedRoot -Recurse -Force
}
if (Test-Path -LiteralPath $Destination) {
    Remove-Item -LiteralPath $Destination -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $expandedRoot, $Destination | Out-Null

& expand.exe $ArchivePath "-F:*" $expandedRoot | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Could not extract fixed WebView2 runtime archive."
}

$runtimeRoot = Get-ChildItem -LiteralPath $expandedRoot -Directory | Select-Object -First 1
if (-not $runtimeRoot) {
    throw "Fixed WebView2 archive did not contain a runtime directory."
}
Copy-Item -Path (Join-Path $runtimeRoot.FullName "*") -Destination $Destination -Recurse -Force
Remove-Item -LiteralPath $expandedRoot -Recurse -Force

if (-not (Test-Path -LiteralPath (Join-Path $Destination "msedgewebview2.exe"))) {
    throw "Fixed WebView2 runtime is missing msedgewebview2.exe."
}

Write-Host "Prepared fixed WebView2 runtime in $Destination"
