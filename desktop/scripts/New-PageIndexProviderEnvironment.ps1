param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [string]$PythonVersion = "3.11"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$lockFile = Join-Path $repoRoot "requirements-pageindex-experimental.lock"
$worker = Join-Path $repoRoot "openkb/page_tree/pageindex/worker.py"
$runtimeDir = [System.IO.Path]::GetFullPath($Destination)

uv venv $runtimeDir --python $PythonVersion
$python = Join-Path $runtimeDir "Scripts/python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "The isolated PageIndex Python runtime was not created: $python"
}
uv pip install --python $python --no-deps --requirement $lockFile
& $python $worker --check
if ($LASTEXITCODE -ne 0) {
    throw "The isolated PageIndex runtime did not pass its adapter check."
}

Write-Host "PageIndex evaluation runtime: $python"
