[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $DestinationDirectory
)

$ErrorActionPreference = "Stop"

function Get-PinnedDownload {
    param(
        [Parameter(Mandatory = $true)] [string] $Uri,
        [Parameter(Mandatory = $true)] [string] $Destination,
        [Parameter(Mandatory = $true)] [string] $ExpectedHash,
        [Parameter(Mandatory = $true)] [int64] $ExpectedBytes
    )

    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        $existing = Get-Item -LiteralPath $Destination
        $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($existing.Length -eq $ExpectedBytes -and $actual -eq $ExpectedHash.ToLowerInvariant()) {
            return
        }
        Remove-Item -LiteralPath $Destination -Force
    }

    $partial = "$Destination.partial"
    if (Test-Path -LiteralPath $partial) {
        Remove-Item -LiteralPath $partial -Force
    }
    Invoke-WebRequest -Uri $Uri -OutFile $partial
    $actualItem = Get-Item -LiteralPath $partial
    $actualHash = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualItem.Length -ne $ExpectedBytes -or $actualHash -ne $ExpectedHash.ToLowerInvariant()) {
        Remove-Item -LiteralPath $partial -Force
        throw "Downloaded DeepDoc runtime file does not match its pinned manifest: $Uri"
    }
    Move-Item -LiteralPath $partial -Destination $Destination -Force
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$manifestPath = Join-Path $repoRoot "desktop\deepdoc-runtime.json"
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.schemaVersion -ne 1 -or $manifest.runtime -ne "InfiniFlow DeepDoc OCR ONNX") {
    throw "DeepDoc runtime manifest has an unsupported schema."
}
if ($manifest.files.Count -lt 1) {
    throw "DeepDoc runtime manifest has no model files."
}

$DestinationDirectory = [System.IO.Path]::GetFullPath($DestinationDirectory)
New-Item -ItemType Directory -Force -Path $DestinationDirectory | Out-Null
foreach ($file in @($manifest.files)) {
    foreach ($property in @("path", "url", "sha256", "bytes")) {
        if (-not ($file.PSObject.Properties.Name -contains $property)) {
            throw "DeepDoc runtime manifest file record is missing $property."
        }
    }
    if ($file.path -isnot [string] -or [System.IO.Path]::GetFileName($file.path) -ne $file.path) {
        throw "DeepDoc runtime manifest contains an invalid filename."
    }
    if ($file.url -isnot [string] -or -not $file.url.StartsWith("https://", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "DeepDoc runtime manifest contains an invalid download URL."
    }
    if ($file.sha256 -isnot [string] -or $file.sha256 -notmatch "^[0-9a-fA-F]{64}$") {
        throw "DeepDoc runtime manifest contains an invalid SHA-256 value."
    }
    if ($file.bytes -isnot [int] -and $file.bytes -isnot [int64]) {
        throw "DeepDoc runtime manifest contains an invalid byte count."
    }
    Get-PinnedDownload `
        -Uri $file.url `
        -Destination (Join-Path $DestinationDirectory $file.path) `
        -ExpectedHash $file.sha256 `
        -ExpectedBytes ([int64] $file.bytes)
}

Write-Output $DestinationDirectory
