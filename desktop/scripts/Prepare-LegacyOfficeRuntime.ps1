[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $DestinationDirectory
)

$ErrorActionPreference = "Stop"

$tikaVersion = "3.3.2"
$tikaJarName = "tika-server-standard-$tikaVersion.jar"
$tikaUri = "https://downloads.apache.org/tika/$tikaVersion/$tikaJarName"
$tikaSha512 = "fb1f2fe57ac458b09d44d41d816f582e1d2fc93488acff6275caf414d8d5ef94e42166edc0b488dc2fb6ef3aa21fab62b107c43b9060385ff6d675e393c2c9e9"
$javaArchiveName = "OpenJDK17U-jre_x64_windows_hotspot_17.0.16_8.zip"
$javaUri = "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.16%2B8/$javaArchiveName"
$javaSha256 = "d35b05f4832215d8877d0dbf15c6370c854d7d5b812f890a9c0db8ad412a6bf2"

function Get-PinnedDownload {
    param(
        [Parameter(Mandatory = $true)] [string] $Uri,
        [Parameter(Mandatory = $true)] [string] $Destination,
        [Parameter(Mandatory = $true)] [string] $Algorithm,
        [Parameter(Mandatory = $true)] [string] $ExpectedHash
    )

    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        $actual = (Get-FileHash -LiteralPath $Destination -Algorithm $Algorithm).Hash.ToLowerInvariant()
        if ($actual -eq $ExpectedHash.ToLowerInvariant()) {
            return
        }
        Remove-Item -LiteralPath $Destination -Force
    }

    $partial = "$Destination.partial"
    if (Test-Path -LiteralPath $partial) {
        Remove-Item -LiteralPath $partial -Force
    }
    Invoke-WebRequest -Uri $Uri -OutFile $partial
    $actual = (Get-FileHash -LiteralPath $partial -Algorithm $Algorithm).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedHash.ToLowerInvariant()) {
        Remove-Item -LiteralPath $partial -Force
        throw "Downloaded runtime hash does not match the pinned $Algorithm value: $Uri"
    }
    Move-Item -LiteralPath $partial -Destination $Destination -Force
}

$DestinationDirectory = [System.IO.Path]::GetFullPath($DestinationDirectory)
$cacheDirectory = "$DestinationDirectory-cache"
$tikaDirectory = Join-Path $DestinationDirectory "tika"
$javaDirectory = Join-Path $DestinationDirectory "java"
$tikaJar = Join-Path $tikaDirectory $tikaJarName
$javaArchive = Join-Path $cacheDirectory $javaArchiveName

New-Item -ItemType Directory -Force -Path $cacheDirectory, $tikaDirectory | Out-Null
Get-PinnedDownload -Uri $tikaUri -Destination $tikaJar -Algorithm "SHA512" -ExpectedHash $tikaSha512
Get-PinnedDownload -Uri $javaUri -Destination $javaArchive -Algorithm "SHA256" -ExpectedHash $javaSha256

if (-not (Test-Path -LiteralPath (Join-Path $javaDirectory "bin\java.exe") -PathType Leaf)) {
    if (Test-Path -LiteralPath $javaDirectory) {
        Remove-Item -LiteralPath $javaDirectory -Recurse -Force
    }
    $expandedJava = Join-Path $cacheDirectory "java-expanded"
    if (Test-Path -LiteralPath $expandedJava) {
        Remove-Item -LiteralPath $expandedJava -Recurse -Force
    }
    Expand-Archive -LiteralPath $javaArchive -DestinationPath $expandedJava -Force
    $roots = @(Get-ChildItem -LiteralPath $expandedJava -Directory)
    if ($roots.Count -ne 1) {
        throw "Pinned Temurin archive has an unexpected directory layout."
    }
    Move-Item -LiteralPath $roots[0].FullName -Destination $javaDirectory
}

if (-not (Test-Path -LiteralPath $tikaJar -PathType Leaf)) {
    throw "Pinned Tika Server JAR is missing after preparation."
}
if (-not (Test-Path -LiteralPath (Join-Path $javaDirectory "bin\java.exe") -PathType Leaf)) {
    throw "Pinned Java Runtime is missing java.exe after preparation."
}

Write-Output $DestinationDirectory
