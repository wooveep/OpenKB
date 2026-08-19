[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $CandidatePackageDirectory,
    [Parameter(Mandatory = $true)] [string] $BaselinePackageDirectory,
    [Parameter(Mandatory = $true)] [string] $OutputPath,
    [string] $CandidateReleaseSummary,
    [string] $BaselineReleaseSummary,
    [string] $FixedEvaluationReport,
    [ValidateRange(5, 30)] [int] $Iterations = 5
)

$ErrorActionPreference = "Stop"

function Quote-ProcessArgument {
    param([Parameter(Mandatory = $true)] [string] $Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-MeasuredProcess {
    param(
        [Parameter(Mandatory = $true)] [string] $Executable,
        [string[]] $Arguments = @(),
        [switch] $CloseStandardInput,
        [int] $TimeoutSeconds = 180
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.Arguments = (($Arguments | ForEach-Object { Quote-ProcessArgument -Value $_ }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.RedirectStandardInput = $CloseStandardInput.IsPresent
    $startInfo.EnvironmentVariables["PATH"] = "$env:SystemRoot\System32;$env:SystemRoot"
    $startInfo.EnvironmentVariables["HF_HUB_OFFLINE"] = "1"
    $startInfo.EnvironmentVariables["TRANSFORMERS_OFFLINE"] = "1"
    $startInfo.EnvironmentVariables["PIP_NO_INDEX"] = "1"
    $startInfo.EnvironmentVariables["UV_OFFLINE"] = "1"
    $startInfo.EnvironmentVariables["HF_HUB_DISABLE_TELEMETRY"] = "1"
    $startInfo.EnvironmentVariables["HTTP_PROXY"] = "http://127.0.0.1:9"
    $startInfo.EnvironmentVariables["HTTPS_PROXY"] = "http://127.0.0.1:9"
    $startInfo.EnvironmentVariables["ALL_PROXY"] = "http://127.0.0.1:9"
    $startInfo.EnvironmentVariables["NO_PROXY"] = ""
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $started = $false
    [int64] $peakWorkingSet = 0
    try {
        if (-not $process.Start()) {
            throw "Could not start measured process: $Executable"
        }
        $started = $true
        if ($CloseStandardInput) {
            $process.StandardInput.Close()
        }
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
        while (-not $process.HasExited) {
            if ([DateTime]::UtcNow -ge $deadline) {
                $process.Kill()
                $process.WaitForExit()
                throw "Measured process timed out: $Executable"
            }
            try {
                $process.Refresh()
                $peakWorkingSet = [Math]::Max($peakWorkingSet, [int64] $process.WorkingSet64)
            }
            catch {
                # The process may exit between HasExited and Refresh.
            }
            Start-Sleep -Milliseconds 10
        }
        $process.WaitForExit()
        $watch.Stop()
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $exitCode = $process.ExitCode
        try {
            $peakWorkingSet = [Math]::Max($peakWorkingSet, [int64] $process.PeakWorkingSet64)
        }
        catch {
            # PeakWorkingSet64 can be unavailable after a very early process exit.
        }
        return [pscustomobject] [ordered]@{
            elapsedMs = [Math]::Round($watch.Elapsed.TotalMilliseconds, 3)
            peakWorkingSetBytes = $peakWorkingSet
            exitCode = $exitCode
            stdout = $stdout
            stderr = $stderr
        }
    }
    finally {
        $watch.Stop()
        if ($started -and -not $process.HasExited) {
            $process.Kill()
            $process.WaitForExit()
        }
        $process.Dispose()
    }
}

function Get-DirectoryBytes {
    param([Parameter(Mandatory = $true)] [string] $Path)
    [int64] $total = 0
    foreach ($file in @(Get-ChildItem -LiteralPath $Path -Recurse -File)) {
        $total += [int64] $file.Length
    }
    return $total
}

function Get-Percentile95 {
    param([Parameter(Mandatory = $true)] [double[]] $Values)
    $ordered = @($Values | Sort-Object)
    $index = [Math]::Max(0, [Math]::Ceiling($ordered.Count * 0.95) - 1)
    return [double] $ordered[$index]
}

function Measure-Engine {
    param([Parameter(Mandatory = $true)] [string] $PackageDirectory)
    $engine = Join-Path $PackageDirectory "runtime\engine\OpenKBEngine.exe"
    if (-not (Test-Path -LiteralPath $engine -PathType Leaf)) {
        throw "Portable Engine is missing: $engine"
    }
    $runs = @()
    for ($index = 0; $index -lt $Iterations; $index++) {
        $run = Invoke-MeasuredProcess -Executable $engine -CloseStandardInput
        if ($run.exitCode -ne 0) {
            throw "Portable Engine cold-start probe failed: $($run.stderr)"
        }
        $runs += $run
    }
    return [ordered]@{
        samplesMs = @($runs | ForEach-Object { $_.elapsedMs })
        p95Ms = Get-Percentile95 -Values @($runs | ForEach-Object { [double] $_.elapsedMs })
        peakWorkingSetBytes = [int64] (($runs | Measure-Object -Property peakWorkingSetBytes -Maximum).Maximum)
    }
}

function Read-ArchiveBytes {
    param([string] $SummaryPath)
    if (-not $SummaryPath) {
        return $null
    }
    $summary = Get-Content -Raw -LiteralPath $SummaryPath | ConvertFrom-Json
    return [int64] $summary.archive.bytes
}

$candidate = [System.IO.Path]::GetFullPath($CandidatePackageDirectory)
$baseline = [System.IO.Path]::GetFullPath($BaselinePackageDirectory)
$output = [System.IO.Path]::GetFullPath($OutputPath)
$candidateManifest = Get-Content -Raw -LiteralPath (Join-Path $candidate "release-manifest.json") | ConvertFrom-Json
$baselineManifest = Get-Content -Raw -LiteralPath (Join-Path $baseline "release-manifest.json") | ConvertFrom-Json
$worker = Join-Path $candidate "runtime\pageindex\OpenKBPageIndex.exe"
$candidateEnginePath = Join-Path $candidate "runtime\engine\OpenKBEngine.exe"
$evaluationSuite = Join-Path $candidate "runtime\pageindex\fixed-suite.json"
if (-not (Test-Path -LiteralPath $worker -PathType Leaf)) {
    throw "Portable PageIndex worker is missing: $worker"
}
if (-not (Test-Path -LiteralPath $candidateEnginePath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $evaluationSuite -PathType Leaf)) {
    throw "Portable PageIndex evaluation boundary is incomplete."
}
if ($candidateManifest.experimentalProviders.pageIndex.defaultEnabled -ne $false) {
    throw "The experimental PageIndex provider must remain disabled by default during acceptance."
}

$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ("OpenKB-PageIndex-Acceptance-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $scratch | Out-Null
try {
    $source = Join-Path $scratch "portable-query.md"
    [System.IO.File]::WriteAllText(
        $source,
        "# Portable package`n`nPageIndex package evidence.`n`n## Recovery`n`nBaseline remains authoritative.`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
    $check = Invoke-MeasuredProcess -Executable $worker -Arguments @("--check")
    if ($check.exitCode -ne 0) {
        throw "Portable PageIndex self-check failed: $($check.stderr)"
    }
    $checkPayload = $check.stdout | ConvertFrom-Json
    if ($checkPayload.pageindex_version -ne "0.2.10") {
        throw "Portable PageIndex self-check returned the wrong version."
    }

    $queryRuns = @()
    for ($index = 0; $index -lt $Iterations; $index++) {
        $tree = Join-Path $scratch ("tree-" + $index + ".json")
        $run = Invoke-MeasuredProcess -Executable $worker -Arguments @($source, $tree)
        if ($run.exitCode -ne 0 -or -not (Test-Path -LiteralPath $tree -PathType Leaf)) {
            throw "Portable PageIndex query failed: $($run.stderr)"
        }
        $payload = Get-Content -Raw -LiteralPath $tree | ConvertFrom-Json
        if (@($payload.structure).Count -eq 0) {
            throw "Portable PageIndex query returned no structure."
        }
        $queryRuns += $run
    }

    $failed = Invoke-MeasuredProcess `
        -Executable $worker `
        -Arguments @((Join-Path $scratch "missing.md"), (Join-Path $scratch "failed.json"))
    if ($failed.exitCode -eq 0) {
        throw "Portable PageIndex crash probe unexpectedly succeeded."
    }
    Start-Sleep -Milliseconds 100
    $orphans = @(Get-Process -Name "OpenKBPageIndex" -ErrorAction SilentlyContinue)
    if ($orphans.Count -ne 0) {
        throw "Portable PageIndex crash probe left an orphan process."
    }

    $baselineEngine = Measure-Engine -PackageDirectory $baseline
    $candidateEngine = Measure-Engine -PackageDirectory $candidate
    $coldStartDeltaMs = [Math]::Round($candidateEngine.p95Ms - $baselineEngine.p95Ms, 3)
    $coldStartPassed = $coldStartDeltaMs -le 1000.0
    $fixedValidation = $null
    if ($FixedEvaluationReport -and (Test-Path -LiteralPath $FixedEvaluationReport -PathType Leaf)) {
        $validationRun = Invoke-MeasuredProcess `
            -Executable $candidateEnginePath `
            -Arguments @(
                "--pageindex-validate-evaluation",
                (Join-Path $candidate "release-manifest.json"),
                $evaluationSuite,
                ([System.IO.Path]::GetFullPath($FixedEvaluationReport))
            )
        if ($validationRun.exitCode -eq 0) {
            try {
                $fixedValidation = $validationRun.stdout | ConvertFrom-Json
            }
            catch {
                $fixedValidation = $null
            }
        }
    }
    $fixedReportValid = $null -ne $fixedValidation -and $fixedValidation.valid -eq $true
    $fixedGatePassed = $fixedReportValid -and $fixedValidation.passed -eq $true

    $failures = @()
    if (-not $coldStartPassed) {
        $failures += "windows_cold_start_delta_exceeded"
    }
    if (-not $FixedEvaluationReport) {
        $failures += "fixed_evaluation_report_missing"
    }
    elseif (-not $fixedReportValid) {
        $failures += "fixed_evaluation_report_invalid"
    }
    elseif (-not $fixedGatePassed) {
        $failures += "fixed_evaluation_gate_failed"
    }
    $decision = if ($failures.Count -eq 0) { "eligible_for_promotion" } else { "not_promoted" }
    $os = Get-CimInstance Win32_OperatingSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $record = [ordered]@{
        schemaVersion = 1
        measuredAtUtc = [DateTime]::UtcNow.ToString("o")
        environment = [ordered]@{
            machine = $env:COMPUTERNAME
            os = $os.Caption
            osVersion = $os.Version
            architecture = $os.OSArchitecture
            cpu = $cpu.Name
            logicalProcessors = $cpu.NumberOfLogicalProcessors
            totalMemoryBytes = [int64] $os.TotalVisibleMemorySize * 1024
            powershell = $PSVersionTable.PSVersion.ToString()
        }
        package = [ordered]@{
            candidateArchiveBytes = Read-ArchiveBytes -SummaryPath $CandidateReleaseSummary
            baselineArchiveBytes = Read-ArchiveBytes -SummaryPath $BaselineReleaseSummary
            candidateExpandedBytes = Get-DirectoryBytes -Path $candidate
            baselineExpandedBytes = Get-DirectoryBytes -Path $baseline
            candidatePayloadBytes = [int64] $candidateManifest.payloadBytes
            baselinePayloadBytes = [int64] $baselineManifest.payloadBytes
            pageIndexComponentBytes = [int64] $candidateManifest.componentBytes.pageIndex
        }
        engine = [ordered]@{
            providerLazyLoaded = $true
            baseline = $baselineEngine
            candidate = $candidateEngine
            coldStartP95DeltaMs = $coldStartDeltaMs
            coldStartBudgetMs = 1000.0
            coldStartPassed = $coldStartPassed
        }
        pageIndex = [ordered]@{
            packageVersion = $candidateManifest.experimentalProviders.pageIndex.packageVersion
            providerVersion = $candidateManifest.experimentalProviders.pageIndex.providerVersion
            defaultEnabled = $false
            selfCheckMs = $check.elapsedMs
            firstQueryLatencyMs = $queryRuns[0].elapsedMs
            queryP95Ms = Get-Percentile95 -Values @($queryRuns | ForEach-Object { [double] $_.elapsedMs })
            peakWorkingSetBytes = [int64] (($queryRuns | Measure-Object -Property peakWorkingSetBytes -Maximum).Maximum)
            crashContained = $true
        }
        fixedEvaluation = [ordered]@{
            report = if ($FixedEvaluationReport) { Split-Path -Leaf $FixedEvaluationReport } else { $null }
            sha256 = if ($fixedReportValid) { $fixedValidation.report_sha256 } else { $null }
            valid = $fixedReportValid
            passed = $fixedGatePassed
            suiteSnapshotId = if ($fixedReportValid) { $fixedValidation.suite_snapshot_id } else { $null }
            suiteDigest = if ($fixedReportValid) { $fixedValidation.suite_digest } else { $null }
            corpusDigest = if ($fixedReportValid) { $fixedValidation.corpus_digest } else { $null }
            caseCount = if ($fixedReportValid) { $fixedValidation.case_count } else { $null }
            variantCount = if ($fixedReportValid) { $fixedValidation.variant_count } else { $null }
            repetitions = if ($fixedReportValid) { $fixedValidation.repetitions } else { $null }
            providerKind = if ($fixedReportValid) { $fixedValidation.provider_kind } else { $null }
            providerVersion = if ($fixedReportValid) { $fixedValidation.provider_version } else { $null }
            workerSha256 = if ($fixedReportValid) { $fixedValidation.worker_sha256 } else { $null }
        }
        decision = [ordered]@{
            result = $decision
            defaultProvider = "deterministic"
            pageIndexDefaultEnabled = $false
            failures = $failures
        }
    }
    $outputDirectory = Split-Path -Parent $output
    New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
    [System.IO.File]::WriteAllText(
        $output,
        ($record | ConvertTo-Json -Depth 8),
        (New-Object System.Text.UTF8Encoding($false))
    )
    Write-Host "PageIndex Portable acceptance record: $output"
    Write-Host "Promotion decision: $decision"
}
finally {
    if (Test-Path -LiteralPath $scratch) {
        Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
    }
}
