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

function Get-DirectoryBytes {
    param([Parameter(Mandatory = $true)] [string] $Path)

    [int64] $total = 0
    foreach ($file in @(Get-ChildItem -LiteralPath $Path -Recurse -File)) {
        $total += $file.Length
    }
    return $total
}

function Write-ReleaseManifest {
    param(
        [Parameter(Mandatory = $true)] [string] $PackageRoot,
        [Parameter(Mandatory = $true)] [string] $PackageVersion,
        [Parameter(Mandatory = $true)] [string] $PageIndexPackageVersion,
        [Parameter(Mandatory = $true)] [string] $PageIndexSourceCommit,
        [Parameter(Mandatory = $true)] [string] $PageIndexProviderVersion,
        [Parameter(Mandatory = $true)] [string] $EvaluationSuiteSnapshotId,
        [Parameter(Mandatory = $true)] [string] $EvaluationSuiteDigest,
        [Parameter(Mandatory = $true)] [int] $EvaluationCaseCount,
        [Parameter(Mandatory = $true)] [string] $EvaluationCorpusDigest,
        [Parameter(Mandatory = $true)] [string[]] $EvaluationCorpusFiles
    )

    $files = @(
        Get-ChildItem -LiteralPath $PackageRoot -Recurse -File |
            Where-Object { $_.FullName -ne (Join-Path $PackageRoot "openkb.local.json") } |
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
    [int64] $payloadBytes = 0
    foreach ($file in $files) {
        $payloadBytes += [int64] $file.bytes
    }
    $manifest = [ordered]@{
        schemaVersion = 3
        product = "OpenKB"
        version = $PackageVersion
        platform = "windows-x64"
        entryPoint = "OpenKB.exe"
        generatedAtUtc = [DateTime]::UtcNow.ToString("o")
        payloadBytes = $payloadBytes
        componentBytes = [ordered]@{
            shell = (Get-Item -LiteralPath (Join-Path $PackageRoot "OpenKB.exe")).Length
            engine = Get-DirectoryBytes -Path (Join-Path $PackageRoot "runtime\engine")
            webView2 = Get-DirectoryBytes -Path (Join-Path $PackageRoot "runtime\webview2")
            deepdoc = Get-DirectoryBytes -Path (Join-Path $PackageRoot "runtime\engine\_internal\deepdoc")
            legacyOffice = Get-DirectoryBytes -Path (Join-Path $PackageRoot "runtime\engine\_internal\legacy-office")
            pageIndex = Get-DirectoryBytes -Path (Join-Path $PackageRoot "runtime\pageindex")
        }
        experimentalProviders = [ordered]@{
            pageIndex = [ordered]@{
                packaged = $true
                defaultEnabled = $false
                packageVersion = $PageIndexPackageVersion
                sourceCommit = $PageIndexSourceCommit
                providerKind = "official_pageindex"
                providerVersion = $PageIndexProviderVersion
                entryPoint = "runtime/pageindex/OpenKBPageIndex.exe"
                evaluation = [ordered]@{
                    suite = "runtime/pageindex/fixed-suite.json"
                    suiteSnapshotId = $EvaluationSuiteSnapshotId
                    suiteDigest = $EvaluationSuiteDigest
                    caseCount = $EvaluationCaseCount
                    corpusDigest = $EvaluationCorpusDigest
                    corpusFiles = @($EvaluationCorpusFiles)
                    variants = @(
                        "fts",
                        "structure_lexical",
                        "wiki",
                        "baseline",
                        "local_graph",
                        "document_page_tree",
                        "catalog + document_page_tree"
                    )
                }
            }
        }
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
$deepDocBuildDirectory = Join-Path $buildRoot "deepdoc"
if (Test-Path -LiteralPath $deepDocBuildDirectory) {
    Remove-Item -LiteralPath $deepDocBuildDirectory -Recurse -Force
}
$deepDocRuntime = & (Join-Path $PSScriptRoot "Prepare-DeepDocRuntime.ps1") `
    -DestinationDirectory $deepDocBuildDirectory
$legacyOfficeRuntime = & (Join-Path $PSScriptRoot "Prepare-LegacyOfficeRuntime.ps1") `
    -DestinationDirectory (Join-Path $buildRoot "legacy-office")

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

$enginePythonVersion = (& uv run --directory $repoRoot --extra desktop-build python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Desktop Engine Python version check failed with exit code $LASTEXITCODE."
}
if ($enginePythonVersion -ne "3.12") {
    throw "The portable Desktop Engine must be frozen with Python 3.12; found $enginePythonVersion."
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
            --collect-data litellm `
            --collect-submodules tiktoken_ext `
            --collect-data rapidocr_onnxruntime `
            --collect-all tika `
            --add-data "$deepDocRuntime;deepdoc" `
            --add-data "$legacyOfficeRuntime;legacy-office" `
            (Join-Path $repoRoot "openkb\desktop_engine_entrypoint.py")
    }
}
finally {
    Pop-Location
}

$pageIndexSuite = Join-Path $desktopRoot "test-assets\pageindex-evaluation\fixed-suite.json"
$pageIndexIdentityJson = & uv run --directory $repoRoot python -c "import json, sys; from pathlib import Path; from openkb.desktop_pageindex_acceptance import pageindex_evaluation_corpus_identity; from openkb.desktop_pageindex_adapter import PAGEINDEX_PACKAGE_VERSION, PAGEINDEX_PROVIDER_VERSION, PAGEINDEX_SOURCE_COMMIT; from openkb.desktop_retrieval_evaluation_types import DesktopRetrievalEvaluationSuite; path = Path(sys.argv[1]); suite = DesktopRetrievalEvaluationSuite.from_json(path); corpus_digest, corpus_files = pageindex_evaluation_corpus_identity(path); print(json.dumps({'packageVersion': PAGEINDEX_PACKAGE_VERSION, 'providerVersion': PAGEINDEX_PROVIDER_VERSION, 'sourceCommit': PAGEINDEX_SOURCE_COMMIT, 'suiteSnapshotId': suite.snapshot_id, 'suiteDigest': suite.digest, 'caseCount': len(suite.cases), 'corpusDigest': corpus_digest, 'corpusFiles': corpus_files}))" $pageIndexSuite
if ($LASTEXITCODE -ne 0) {
    throw "PageIndex provider identity check failed with exit code $LASTEXITCODE."
}
$pageIndexIdentity = $pageIndexIdentityJson | ConvertFrom-Json
$pageIndexRoot = Join-Path $buildRoot "pageindex"
$pageIndexEnvironment = Join-Path $pageIndexRoot "environment"
$pageIndexDist = Join-Path $pageIndexRoot "dist"
$pageIndexWork = Join-Path $pageIndexRoot "work"
$pageIndexSpec = Join-Path $pageIndexRoot "spec"
if (Test-Path -LiteralPath $pageIndexRoot) {
    Remove-Item -LiteralPath $pageIndexRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $pageIndexRoot, $pageIndexDist, $pageIndexWork, $pageIndexSpec | Out-Null

Invoke-Checked "PageIndex build environment" { & uv venv $pageIndexEnvironment --python $enginePythonVersion }
$pageIndexPython = Join-Path $pageIndexEnvironment "Scripts\python.exe"
Invoke-Checked "PageIndex runtime dependency install" {
    & uv pip install `
        --python $pageIndexPython `
        --no-deps `
        --requirement (Join-Path $repoRoot "requirements-pageindex-experimental.lock")
}
Invoke-Checked "PageIndex build dependency install" {
    & uv pip install `
        --python $pageIndexPython `
        --requirement (Join-Path $repoRoot "requirements-pageindex-build.lock")
}
Invoke-Checked "PageIndex isolated runtime check" {
    & $pageIndexPython (Join-Path $repoRoot "openkb\desktop_pageindex_worker.py") --check
}

Push-Location $repoRoot
try {
    Invoke-Checked "PageIndex worker freeze" {
        & $pageIndexPython -m PyInstaller `
            --noconfirm `
            --clean `
            --onedir `
            --name OpenKBPageIndex `
            --distpath $pageIndexDist `
            --workpath $pageIndexWork `
            --specpath $pageIndexSpec `
            --hidden-import pageindex.page_index_md `
            --collect-data pageindex `
            --copy-metadata pageindex `
            --copy-metadata PyPDF2 `
            --copy-metadata python-dotenv `
            --copy-metadata PyYAML `
            (Join-Path $repoRoot "openkb\desktop_pageindex_worker.py")
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
$pageIndexDirectory = Join-Path $pageIndexDist "OpenKBPageIndex"
$pageIndexExe = Join-Path $pageIndexDirectory "OpenKBPageIndex.exe"
$webViewRuntime = Join-Path $srcTauri "runtime\webview2"
foreach ($requiredPath in @(
    $shellExe,
    $engineExe,
    $pageIndexExe,
    (Join-Path $webViewRuntime "msedgewebview2.exe"),
    (Join-Path $engineDirectory "_internal\rapidocr_onnxruntime\config.yaml"),
    (Join-Path $engineDirectory "_internal\rapidocr_onnxruntime\models\ch_PP-OCRv4_det_infer.onnx"),
    (Join-Path $engineDirectory "_internal\rapidocr_onnxruntime\models\ch_PP-OCRv4_rec_infer.onnx"),
    (Join-Path $engineDirectory "_internal\rapidocr_onnxruntime\models\ch_ppocr_mobile_v2.0_cls_infer.onnx"),
    (Join-Path $engineDirectory "_internal\litellm\model_prices_and_context_window_backup.json"),
    (Join-Path $engineDirectory "_internal\deepdoc\det.onnx"),
    (Join-Path $engineDirectory "_internal\deepdoc\rec.onnx"),
    (Join-Path $engineDirectory "_internal\deepdoc\ocr.res"),
    (Join-Path $engineDirectory "_internal\legacy-office\tika\tika-server-standard-3.3.2.jar"),
    (Join-Path $engineDirectory "_internal\legacy-office\java\bin\java.exe")
)) {
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
Copy-DirectoryContents -Source $pageIndexDirectory -Destination (Join-Path $packageRoot "runtime\pageindex")
Copy-DirectoryContents -Source $webViewRuntime -Destination (Join-Path $packageRoot "runtime\webview2")
Copy-Item -LiteralPath (Join-Path $repoRoot "requirements-pageindex-experimental.lock") -Destination (Join-Path $packageRoot "runtime\pageindex") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "requirements-pageindex-build.lock") -Destination (Join-Path $packageRoot "runtime\pageindex") -Force
Copy-Item -LiteralPath (Join-Path $desktopRoot "licenses\PageIndex-MIT.txt") -Destination (Join-Path $packageRoot "runtime\pageindex") -Force
Copy-Item -LiteralPath $pageIndexSuite -Destination (Join-Path $packageRoot "runtime\pageindex\fixed-suite.json") -Force
foreach ($corpusFile in @($pageIndexIdentity.corpusFiles)) {
    Copy-Item -LiteralPath (Join-Path (Split-Path -Parent $pageIndexSuite) $corpusFile) -Destination (Join-Path $packageRoot "runtime\pageindex") -Force
}
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination $packageRoot -Force
Copy-Item -LiteralPath (Join-Path $desktopRoot "THIRD_PARTY_NOTICES.md") -Destination $packageRoot -Force
Copy-Item -LiteralPath (Join-Path $desktopRoot "openkb.local.example.json") -Destination $packageRoot -Force
Write-ReleaseManifest `
    -PackageRoot $packageRoot `
    -PackageVersion $Version `
    -PageIndexPackageVersion $pageIndexIdentity.packageVersion `
    -PageIndexSourceCommit $pageIndexIdentity.sourceCommit `
    -PageIndexProviderVersion $pageIndexIdentity.providerVersion `
    -EvaluationSuiteSnapshotId $pageIndexIdentity.suiteSnapshotId `
    -EvaluationSuiteDigest $pageIndexIdentity.suiteDigest `
    -EvaluationCaseCount $pageIndexIdentity.caseCount `
    -EvaluationCorpusDigest $pageIndexIdentity.corpusDigest `
    -EvaluationCorpusFiles @($pageIndexIdentity.corpusFiles)

$zipPath = Join-Path $OutputDirectory "$packageName.zip"
$checksumPath = "$zipPath.sha256"
$summaryPath = Join-Path $OutputDirectory "$packageName.release.json"
foreach ($existingOutput in @($zipPath, $checksumPath, $summaryPath)) {
    if (Test-Path -LiteralPath $existingOutput) {
        Remove-Item -LiteralPath $existingOutput -Force
    }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
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
$releaseSummary = [ordered]@{
    schemaVersion = 1
    product = "OpenKB"
    version = $Version
    platform = "windows-x64"
    archive = [ordered]@{
        file = [System.IO.Path]::GetFileName($zipPath)
        bytes = (Get-Item -LiteralPath $zipPath).Length
        sha256 = $zipHash
    }
    payload = [ordered]@{
        bytes = ((Get-Content -Raw -LiteralPath (Join-Path $packageRoot "release-manifest.json") | ConvertFrom-Json).payloadBytes)
        manifest = "release-manifest.json"
    }
}
[System.IO.File]::WriteAllText(
    $summaryPath,
    ($releaseSummary | ConvertTo-Json -Depth 5),
    (New-Object System.Text.UTF8Encoding($false))
)

Write-Host "Portable package created: $zipPath"
Write-Host "SHA-256 file created: $checksumPath"
Write-Host "Release size summary created: $summaryPath"
