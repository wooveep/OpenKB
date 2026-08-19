$ErrorActionPreference = "Stop"

function Assert-PageIndexCondition {
    param(
        [Parameter(Mandatory = $true)] [bool] $Condition,
        [Parameter(Mandatory = $true)] [string] $Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Quote-PageIndexProcessArgument {
    param([Parameter(Mandatory = $true)] [string] $Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Set-PageIndexIsolatedEnvironment {
    param([Parameter(Mandatory = $true)] $StartInfo)
    $StartInfo.EnvironmentVariables["PATH"] = "$env:SystemRoot\System32;$env:SystemRoot"
    $StartInfo.EnvironmentVariables["HF_HUB_OFFLINE"] = "1"
    $StartInfo.EnvironmentVariables["TRANSFORMERS_OFFLINE"] = "1"
    $StartInfo.EnvironmentVariables["PIP_NO_INDEX"] = "1"
    $StartInfo.EnvironmentVariables["UV_OFFLINE"] = "1"
    $StartInfo.EnvironmentVariables["PYTHONNOUSERSITE"] = "1"
    $StartInfo.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1"
    $StartInfo.EnvironmentVariables["HF_HUB_DISABLE_TELEMETRY"] = "1"
    $StartInfo.EnvironmentVariables["HTTP_PROXY"] = "http://127.0.0.1:9"
    $StartInfo.EnvironmentVariables["HTTPS_PROXY"] = "http://127.0.0.1:9"
    $StartInfo.EnvironmentVariables["ALL_PROXY"] = "http://127.0.0.1:9"
    $StartInfo.EnvironmentVariables["NO_PROXY"] = ""
}

function Invoke-BoundedPageIndexProcess {
    param(
        [Parameter(Mandatory = $true)] [string] $Executable,
        [string[]] $Arguments = @(),
        [Parameter(Mandatory = $true)] [string] $WorkingDirectory,
        [int] $TimeoutSeconds = 60
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.Arguments = (($Arguments | ForEach-Object { Quote-PageIndexProcessArgument -Value $_ }) -join ' ')
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    Set-PageIndexIsolatedEnvironment -StartInfo $startInfo
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $started = $false
    try {
        Assert-PageIndexCondition -Condition ($process.Start()) -Message "Could not start packaged PageIndex process: $Executable"
        $started = $true
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill()
            $process.WaitForExit()
            throw "Packaged PageIndex process timed out after $TimeoutSeconds seconds: $Executable"
        }
        $process.WaitForExit()
        return [pscustomobject] [ordered]@{
            exitCode = $process.ExitCode
            stdout = $process.StandardOutput.ReadToEnd()
            stderr = $process.StandardError.ReadToEnd()
        }
    }
    finally {
        if ($started -and -not $process.HasExited) {
            $process.Kill()
            $process.WaitForExit()
        }
        $process.Dispose()
    }
}

function Test-FrozenPageIndexWorker {
    param(
        [Parameter(Mandatory = $true)] [string] $WorkerPath,
        [Parameter(Mandatory = $true)] [string] $ScratchDirectory
    )

    $checkRun = Invoke-BoundedPageIndexProcess `
        -Executable $WorkerPath -Arguments @("--check") -WorkingDirectory $ScratchDirectory
    Assert-PageIndexCondition -Condition ($checkRun.exitCode -eq 0) -Message "Frozen PageIndex worker self-check failed: $($checkRun.stderr)"
    $check = $checkRun.stdout | ConvertFrom-Json
    Assert-PageIndexCondition -Condition ($check.pageindex_version -eq "0.2.10") -Message "Frozen PageIndex worker has the wrong package version."

    $offlineName = "pageindex " + [char]0x79BB + [char]0x7EBF + " input.md"
    $inputPath = Join-Path $ScratchDirectory $offlineName
    $outputPath = Join-Path $ScratchDirectory "pageindex-tree.json"
    [System.IO.File]::WriteAllText(
        $inputPath,
        "# Portable Guide`n`nPortable PageIndex evidence.`n`n## Detail`n`nOffline provider result.`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
    $treeRun = Invoke-BoundedPageIndexProcess `
        -Executable $WorkerPath -Arguments @($inputPath, $outputPath) -WorkingDirectory $ScratchDirectory
    Assert-PageIndexCondition -Condition ($treeRun.exitCode -eq 0) -Message "Frozen PageIndex worker could not build an offline Markdown tree: $($treeRun.stderr)"
    Assert-PageIndexCondition -Condition (Test-Path -LiteralPath $outputPath -PathType Leaf) -Message "Frozen PageIndex worker did not write its tree."
    $tree = Get-Content -Raw -LiteralPath $outputPath | ConvertFrom-Json
    Assert-PageIndexCondition -Condition ($null -ne $tree.structure) -Message "Frozen PageIndex worker returned an invalid tree shape."
    Assert-PageIndexCondition -Condition (@($tree.structure).Count -gt 0) -Message "Frozen PageIndex worker returned an empty tree."

    $missingInput = Join-Path $ScratchDirectory "missing-pageindex-input.md"
    $failedOutput = Join-Path $ScratchDirectory "failed-pageindex-tree.json"
    $failedRun = Invoke-BoundedPageIndexProcess `
        -Executable $WorkerPath -Arguments @($missingInput, $failedOutput) -WorkingDirectory $ScratchDirectory
    Assert-PageIndexCondition -Condition ($failedRun.exitCode -ne 0) -Message "Frozen PageIndex worker unexpectedly accepted a missing input."
    Assert-NoPageIndexWorker -WorkerPath $WorkerPath
}

function Test-PackagedPageIndexAdapter {
    param(
        [Parameter(Mandatory = $true)] [string] $EnginePath,
        [Parameter(Mandatory = $true)] [string] $WorkerPath,
        [Parameter(Mandatory = $true)] [string] $ScratchDirectory
    )

    $acceptanceRoot = Join-Path $ScratchDirectory "pageindex-adapter-acceptance"
    $run = Invoke-BoundedPageIndexProcess `
        -Executable $EnginePath `
        -Arguments @("--pageindex-package-acceptance", $WorkerPath, $acceptanceRoot) `
        -WorkingDirectory $ScratchDirectory `
        -TimeoutSeconds 120
    Assert-PageIndexCondition -Condition ($run.exitCode -eq 0) -Message "Packaged PageIndex adapter acceptance failed: $($run.stderr)"
    $result = $run.stdout | ConvertFrom-Json
    Assert-PageIndexCondition -Condition ($result.schema_version -eq 1 -and $result.passed -eq $true) -Message "Packaged PageIndex adapter returned an invalid result."
    Assert-PageIndexCondition -Condition ($result.provider_kind -eq "official_pageindex") -Message "Packaged PageIndex adapter returned the wrong provider."
    $expected = [ordered]@{
        timeout = "pageindex_provider_timeout"
        invalid_tree = "pageindex_provider_invalid_tree"
        cache_corruption = "rebuilt"
        provider_crash = "pageindex_provider_unavailable"
        baseline_available = $true
        sqlite_integrity = $true
    }
    foreach ($name in $expected.Keys) {
        Assert-PageIndexCondition -Condition ($result.scenarios.$name -eq $expected[$name]) -Message "Packaged PageIndex adapter scenario failed: $name"
    }
    Assert-NoPageIndexWorker -WorkerPath $WorkerPath
}

function Assert-NoPageIndexWorker {
    param([Parameter(Mandatory = $true)] [string] $WorkerPath)
    Start-Sleep -Milliseconds 100
    $workerName = [System.IO.Path]::GetFileNameWithoutExtension($WorkerPath)
    $remaining = @(Get-Process -Name $workerName -ErrorAction SilentlyContinue)
    Assert-PageIndexCondition -Condition ($remaining.Count -eq 0) -Message "Frozen PageIndex worker left an orphan process after failure."
}
