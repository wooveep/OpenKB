[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $PackageDirectory
)

$ErrorActionPreference = "Stop"

function Assert-That {
    param(
        [Parameter(Mandatory = $true)] [bool] $Condition,
        [Parameter(Mandatory = $true)] [string] $Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Get-DirectoryBytes {
    param([Parameter(Mandatory = $true)] [string] $Path)

    [int64] $total = 0
    foreach ($file in @(Get-ChildItem -LiteralPath $Path -Recurse -File)) {
        $total += $file.Length
    }
    return $total
}

function Read-Exact {
    param(
        [Parameter(Mandatory = $true)] [System.IO.Stream] $Stream,
        [Parameter(Mandatory = $true)] [int] $Length,
        [int] $TimeoutMilliseconds = 10000
    )

    $buffer = New-Object byte[] $Length
    $offset = 0
    while ($offset -lt $Length) {
        $readTask = $Stream.ReadAsync($buffer, $offset, $Length - $offset)
        if (-not $readTask.Wait($TimeoutMilliseconds)) {
            throw "Timed out while reading the frozen Engine protocol."
        }
        $read = $readTask.GetAwaiter().GetResult()
        if ($read -le 0) {
            throw "Frozen Engine closed its protocol stream unexpectedly."
        }
        $offset += $read
    }
    Write-Output -NoEnumerate $buffer
}

function Write-Frame {
    param(
        [Parameter(Mandatory = $true)] [System.IO.Stream] $Stream,
        [Parameter(Mandatory = $true)] [hashtable] $Message
    )

    $payload = [System.Text.Encoding]::UTF8.GetBytes(($Message | ConvertTo-Json -Depth 12 -Compress))
    $length = $payload.Length
    $header = [byte[]]@(
        [byte](($length -shr 24) -band 0xff),
        [byte](($length -shr 16) -band 0xff),
        [byte](($length -shr 8) -band 0xff),
        [byte]($length -band 0xff)
    )
    $Stream.Write($header, 0, $header.Length)
    $Stream.Write($payload, 0, $payload.Length)
    $Stream.Flush()
}

function Read-Frame {
    param(
        [Parameter(Mandatory = $true)] [System.IO.Stream] $Stream,
        [int] $TimeoutMilliseconds = 10000
    )

    [byte[]] $header = Read-Exact -Stream $Stream -Length 4 -TimeoutMilliseconds $TimeoutMilliseconds
    $length =
        (([int] $header[0]) -shl 24) -bor
        (([int] $header[1]) -shl 16) -bor
        (([int] $header[2]) -shl 8) -bor
        ([int] $header[3])
    Assert-That -Condition ($length -ge 0 -and $length -le 16777216) -Message "Frozen Engine emitted an invalid frame size."
    [byte[]] $payload = Read-Exact -Stream $Stream -Length $length -TimeoutMilliseconds $TimeoutMilliseconds
    return ([System.Text.Encoding]::UTF8.GetString($payload) | ConvertFrom-Json)
}

function Read-Response {
    param(
        [Parameter(Mandatory = $true)] [System.IO.Stream] $Stream,
        [Parameter(Mandatory = $true)] [string] $RequestId,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [System.Collections.Generic.List[object]] $Events,
        [int] $TimeoutSeconds = 15
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $remaining = [Math]::Max(1, [int] ($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        $message = Read-Frame -Stream $Stream -TimeoutMilliseconds $remaining
        if ($message.PSObject.Properties.Name -contains "method") {
            $Events.Add($message)
            continue
        }
        if ([string] $message.id -eq $RequestId) {
            return $message
        }
    }
    throw "Frozen Engine did not return request $RequestId."
}

function Read-RequestEvent {
    param(
        [Parameter(Mandatory = $true)] [System.IO.Stream] $Stream,
        [Parameter(Mandatory = $true)] [string] $RequestId,
        [Parameter(Mandatory = $true)] [string] $Kind,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [System.Collections.Generic.List[object]] $Events
    )

    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while ([DateTime]::UtcNow -lt $deadline) {
        $remaining = [Math]::Max(1, [int] ($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        $message = Read-Frame -Stream $Stream -TimeoutMilliseconds $remaining
        if ($message.PSObject.Properties.Name -notcontains "method") {
            throw "Frozen Engine returned a response before $Kind for $RequestId."
        }
        $Events.Add($message)
        if ($message.method -eq "event" -and
            $message.params.kind -eq $Kind -and
            [string] $message.params.data.request_id -eq $RequestId) {
            return $message
        }
    }
    throw "Frozen Engine did not stream $Kind for $RequestId."
}

function Assert-SuccessResponse {
    param(
        [Parameter(Mandatory = $true)] $Response,
        [Parameter(Mandatory = $true)] [string] $RequestId
    )

    if ($Response.PSObject.Properties.Name -contains "error") {
        throw "Frozen Engine request $RequestId failed: $($Response.error | ConvertTo-Json -Compress)"
    }
    Assert-That -Condition ($Response.PSObject.Properties.Name -contains "result") -Message "Frozen Engine request $RequestId returned no result."
}

function Test-FrozenEngine {
    param(
        [Parameter(Mandatory = $true)] [string] $EnginePath,
        [Parameter(Mandatory = $true)] [string] $WorkingDirectory,
        [Parameter(Mandatory = $true)] [string] $ScratchDirectory
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $EnginePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    # The Engine reserves stdout for framed protocol messages. Let stderr inherit
    # the build log so ONNX diagnostics cannot fill an unread redirected pipe.
    $startInfo.RedirectStandardError = $false
    $startInfo.Environment["PATH"] = "$env:SystemRoot\System32;$env:SystemRoot"
    $startInfo.Environment["HF_HUB_OFFLINE"] = "1"
    $startInfo.Environment["TRANSFORMERS_OFFLINE"] = "1"
    $startInfo.Environment["PIP_NO_INDEX"] = "1"
    $startInfo.Environment["UV_OFFLINE"] = "1"
    $engine = New-Object System.Diagnostics.Process
    $engine.StartInfo = $startInfo
    Assert-That -Condition ($engine.Start()) -Message "Could not start frozen OpenKB Engine."

    try {
        $input = $engine.StandardInput.BaseStream
        $output = $engine.StandardOutput.BaseStream
        $events = New-Object "System.Collections.Generic.List[object]"

        Write-Frame -Stream $input -Message @{
            jsonrpc = "2.0"; id = "package-handshake"; method = "engine.handshake"; params = @{ protocol_version = 1 }
        }
        $handshake = Read-Response -Stream $output -RequestId "package-handshake" -Events $events
        Assert-SuccessResponse -Response $handshake -RequestId "package-handshake"
        Assert-That -Condition ($handshake.result.protocol_version -eq 1) -Message "Frozen Engine protocol version is incorrect."

        Write-Frame -Stream $input -Message @{
            jsonrpc = "2.0"; id = "package-health"; method = "engine.health"; params = @{}
        }
        $health = Read-Response -Stream $output -RequestId "package-health" -Events $events
        Assert-SuccessResponse -Response $health -RequestId "package-health"
        Assert-That -Condition ($health.result.status -eq "ready") -Message "Frozen Engine health check did not report ready."

        $knowledgeBase = Join-Path $ScratchDirectory "cancel-kb"
        $openkbDirectory = Join-Path $knowledgeBase ".openkb"
        Write-Frame -Stream $input -Message @{
            jsonrpc = "2.0"; id = "package-create"; method = "workbench.create_knowledge_base"; params = @{ kb_dir = $knowledgeBase; name = "Cancel test" }
        }
        $created = Read-Response -Stream $output -RequestId "package-create" -Events $events
        Assert-SuccessResponse -Response $created -RequestId "package-create"

        $lockStream = [System.IO.File]::Open(
            (Join-Path $openkbDirectory "ingest.lock"),
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::ReadWrite
        )
        $lockStream.SetLength(1)
        $lockStream.Lock(0, 1)
        try {
            Write-Frame -Stream $input -Message @{
                jsonrpc = "2.0"; id = "package-read"; method = "workbench.knowledge_pages"; params = @{}
            }
            Read-RequestEvent -Stream $output -RequestId "package-read" -Kind "engine.request_started" -Events $events | Out-Null

            Write-Frame -Stream $input -Message @{
                jsonrpc = "2.0"; id = "package-cancel"; method = "engine.cancel"; params = @{ request_id = "package-read" }
            }
            $cancel = Read-Response -Stream $output -RequestId "package-cancel" -Events $events
            Assert-SuccessResponse -Response $cancel -RequestId "package-cancel"
            Assert-That -Condition ($cancel.result.cancelled -eq $true) -Message "Frozen Engine did not cancel an active request."
        }
        finally {
            $lockStream.Unlock(0, 1)
            $lockStream.Dispose()
        }
        $cancelledRead = Read-Response -Stream $output -RequestId "package-read" -Events $events
        Assert-That -Condition ($cancelledRead.PSObject.Properties.Name -contains "error") -Message "Cancelled Engine request unexpectedly succeeded."
        Assert-That -Condition ($cancelledRead.error.code -eq "request_cancelled") -Message "Cancelled Engine request returned the wrong error."
        $cancelledEventCount = @(
            $events | Where-Object {
                $_.method -eq "event" -and
                $_.params.kind -eq "engine.request_cancelled" -and
                [string] $_.params.data.request_id -eq "package-read"
            }
        ).Count
        Assert-That -Condition ($cancelledEventCount -gt 0) -Message "Frozen Engine did not stream the cancellation event."

        $sourceImage = Join-Path $ScratchDirectory "package-source.png"
        $sourceMarkdown = Join-Path $ScratchDirectory "package-source.md"
        [System.IO.File]::WriteAllBytes(
            $sourceImage,
            [Convert]::FromBase64String("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL0aQAAAABJRU5ErkJggg==")
        )
        [System.IO.File]::WriteAllText(
            $sourceMarkdown,
            "# Package import`n`n![Package image](package-source.png)`n`nOffline package import evidence.`n",
            (New-Object System.Text.UTF8Encoding($false))
        )
        Write-Frame -Stream $input -Message @{
            jsonrpc = "2.0"; id = "package-import"; method = "workbench.import_text_document"; params = @{ source_path = $sourceMarkdown }
        }
        $imported = Read-Response -Stream $output -RequestId "package-import" -Events $events
        Assert-SuccessResponse -Response $imported -RequestId "package-import"
        Assert-That -Condition ($imported.result.job.status -eq "completed") -Message "Frozen Engine did not complete an offline Markdown import."
        Assert-That -Condition ($imported.result.document.availability -eq "available") -Message "Frozen Engine did not publish the offline Markdown import."
        $documentId = [string] $imported.result.document.document_id
        Assert-That -Condition (-not [string]::IsNullOrWhiteSpace($documentId)) -Message "Frozen Engine returned no imported document identifier."

        Write-Frame -Stream $input -Message @{
            jsonrpc = "2.0"; id = "package-read-import"; method = "workbench.read_raw_document"; params = @{ document_id = $documentId; page = 0 }
        }
        $readImported = Read-Response -Stream $output -RequestId "package-read-import" -Events $events
        Assert-SuccessResponse -Response $readImported -RequestId "package-read-import"
        Assert-That -Condition ($readImported.result.name -eq "package-source.md") -Message "Frozen Engine returned the wrong imported source document."
        Assert-That -Condition (@($readImported.result.source_images).Count -eq 1) -Message "Frozen Engine did not preserve a relative Markdown source image."

        $scannedPdf = Join-Path $ScratchDirectory "package-scanned.pdf"
        $scannedPdfFixture = Join-Path $PSScriptRoot "..\test-assets\scanned-ocr.pdf.base64"
        Assert-That -Condition (Test-Path -LiteralPath $scannedPdfFixture -PathType Leaf) -Message "The portable package scan fixture is missing."
        [System.IO.File]::WriteAllBytes(
            $scannedPdf,
            [Convert]::FromBase64String((Get-Content -Raw -LiteralPath $scannedPdfFixture))
        )
        Write-Frame -Stream $input -Message @{
            jsonrpc = "2.0"; id = "package-scan-import"; method = "workbench.import_text_document"; params = @{ source_path = $scannedPdf }
        }
        # First-run ONNX initialization is a packaging smoke check, not the Desktop
        # model-response budget. Allow slower clean Windows machines to load it once.
        Write-Host "Testing frozen scanned-PDF import..."
        $scannedImport = Read-Response -Stream $output -RequestId "package-scan-import" -Events $events -TimeoutSeconds 180
        Assert-SuccessResponse -Response $scannedImport -RequestId "package-scan-import"
        Assert-That -Condition ($scannedImport.result.job.status -eq "completed") -Message "Frozen Engine did not complete an offline scanned-PDF import."
        Assert-That -Condition ($scannedImport.result.document.availability -eq "available") -Message "Frozen Engine did not publish the scanned-PDF import."

        # Keep this last: the deliberately invalid endpoint becomes KB-local
        # configuration, and must not affect the offline parser smoke checks.
        $modelProbeSource = Join-Path $ScratchDirectory "package-model-probe.txt"
        [System.IO.File]::WriteAllText(
            $modelProbeSource,
            "Portable model-runtime probe.",
            (New-Object System.Text.UTF8Encoding($false))
        )
        Write-Frame -Stream $input -Message @{
            jsonrpc = "2.0"; id = "package-model-settings"; method = "workbench.save_model_settings"; params = @{
                model = "gpt-4o-mini"
                api_base_url = "http://127.0.0.1:9/v1"
                api_key = "package-model-probe-key"
                max_concurrent_model_calls = 1
                initial_timeout_seconds = 1
            }
        }
        $modelSettings = Read-Response -Stream $output -RequestId "package-model-settings" -Events $events
        Assert-SuccessResponse -Response $modelSettings -RequestId "package-model-settings"

        Write-Frame -Stream $input -Message @{
            jsonrpc = "2.0"; id = "package-model-import"; method = "workbench.import_text_document"; params = @{ source_path = $modelProbeSource }
        }
        # The Desktop model call has a 60-second logical deadline. Allow a small
        # process/IPC margin while the package proves its dynamic model runtime.
        $modelImport = Read-Response -Stream $output -RequestId "package-model-import" -Events $events -TimeoutSeconds 75
        Assert-That -Condition ($modelImport.PSObject.Properties.Name -contains "error") -Message "Frozen Engine unexpectedly completed the local model-runtime probe."
        Assert-That -Condition ($modelImport.error.code -eq "document_quarantined") -Message "Frozen Engine returned the wrong local model-runtime probe error."

        Write-Frame -Stream $input -Message @{
            jsonrpc = "2.0"; id = "package-model-jobs"; method = "workbench.import_jobs"; params = @{}
        }
        $modelJobs = Read-Response -Stream $output -RequestId "package-model-jobs" -Events $events
        Assert-SuccessResponse -Response $modelJobs -RequestId "package-model-jobs"
        $modelJob = @($modelJobs.result.jobs | Where-Object { $_.job.source_name -eq "package-model-probe.txt" }) | Select-Object -First 1
        Assert-That -Condition ($null -ne $modelJob) -Message "Frozen Engine did not retain the local model-runtime probe job."
        $modelCall = @($modelJob.model_calls) | Select-Object -First 1
        Assert-That -Condition ($null -ne $modelCall) -Message "Frozen Engine did not record a model call for the local model-runtime probe."
        Assert-That -Condition ($modelCall.error_code -in @("model_network_transient", "model_timeout", "model_server_error")) -Message "Frozen Engine model runtime failed before reaching the local endpoint: $($modelCall.error_code)."

        Write-Frame -Stream $input -Message @{
            jsonrpc = "2.0"; id = "package-no-legacy-workbench"; method = "workbench.inspect_knowledge_base"; params = @{}
        }
        $legacyWorkbench = Read-Response -Stream $output -RequestId "package-no-legacy-workbench" -Events $events
        Assert-That -Condition ($legacyWorkbench.PSObject.Properties.Name -contains "error") -Message "Frozen Engine exposed a removed legacy workbench method."
        Assert-That -Condition ($legacyWorkbench.error.code -eq "method_not_found") -Message "Frozen Engine rejected a removed legacy workbench method with the wrong error."

        Write-Frame -Stream $input -Message @{
            jsonrpc = "2.0"; id = "package-shutdown"; method = "engine.shutdown"; params = @{}
        }
        $shutdown = Read-Response -Stream $output -RequestId "package-shutdown" -Events $events
        Assert-SuccessResponse -Response $shutdown -RequestId "package-shutdown"
        Assert-That -Condition ($engine.WaitForExit(10000)) -Message "Frozen Engine did not stop after engine.shutdown."
    }
    finally {
        if (-not $engine.HasExited) {
            Stop-Process -Id $engine.Id -Force -ErrorAction SilentlyContinue
        }
        $engine.Dispose()
    }
}

function Get-DescendantProcesses {
    param([Parameter(Mandatory = $true)] [int] $RootProcessId)

    $allProcesses = @(Get-CimInstance Win32_Process)
    $pendingProcessIds = New-Object "System.Collections.Generic.Queue[int]"
    $descendants = New-Object "System.Collections.Generic.List[object]"
    $pendingProcessIds.Enqueue($RootProcessId)
    while ($pendingProcessIds.Count -gt 0) {
        $parentProcessId = $pendingProcessIds.Dequeue()
        foreach ($process in @($allProcesses | Where-Object { [int] $_.ParentProcessId -eq $parentProcessId })) {
            $descendants.Add($process)
            $pendingProcessIds.Enqueue([int] $process.ProcessId)
        }
    }
    foreach ($descendant in $descendants) {
        Write-Output $descendant
    }
}

function Test-ShellProcessTree {
    param([Parameter(Mandatory = $true)] [string] $PackageRoot)

    $shellPath = Join-Path $PackageRoot "OpenKB.exe"
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $shellPath
    $startInfo.WorkingDirectory = $PackageRoot
    $startInfo.UseShellExecute = $false
    $startInfo.Environment["PATH"] = "$env:SystemRoot\System32;$env:SystemRoot"
    $startInfo.Environment["HF_HUB_OFFLINE"] = "1"
    $startInfo.Environment["TRANSFORMERS_OFFLINE"] = "1"
    $startInfo.Environment["PIP_NO_INDEX"] = "1"
    $startInfo.Environment["UV_OFFLINE"] = "1"
    $shell = New-Object System.Diagnostics.Process
    $shell.StartInfo = $startInfo
    Assert-That -Condition ($shell.Start()) -Message "Could not start OpenKB.exe from the portable package."

    $engineChild = $null
    $webViewChild = $null
    $descendants = @()
    try {
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        while ([DateTime]::UtcNow -lt $deadline) {
            if ($shell.HasExited) {
                throw "OpenKB.exe exited before the portable shell check completed (exit code $($shell.ExitCode))."
            }
            $descendants = @(Get-DescendantProcesses -RootProcessId $shell.Id)
            $engineChild = $descendants | Where-Object { $_.Name -ieq "OpenKBEngine.exe" } | Select-Object -First 1
            $webViewChild = $descendants | Where-Object { $_.Name -ieq "msedgewebview2.exe" } | Select-Object -First 1
            if ($engineChild -and $webViewChild) {
                break
            }
            Start-Sleep -Milliseconds 250
        }
        Assert-That -Condition ($null -ne $engineChild) -Message "OpenKB.exe did not start its packaged OpenKBEngine.exe."
        Assert-That -Condition ($null -ne $webViewChild) -Message "OpenKB.exe did not start the fixed WebView2 runtime."

        $fixedRuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $PackageRoot "runtime\webview2")).TrimEnd([char[]]@('\', '/'))
        $fixedRuntimePrefix = "$fixedRuntimeRoot$([System.IO.Path]::DirectorySeparatorChar)"
        $webViewPath = [System.IO.Path]::GetFullPath([string] $webViewChild.ExecutablePath)
        Assert-That -Condition ($webViewPath.StartsWith($fixedRuntimePrefix, [System.StringComparison]::OrdinalIgnoreCase)) -Message "OpenKB.exe used a system WebView2 runtime instead of the packaged fixed runtime."

        $descendants = @(Get-DescendantProcesses -RootProcessId $shell.Id)
        $trackedProcessIds = @($descendants | ForEach-Object { [int] $_.ProcessId })
        Stop-Process -Id $shell.Id -Force
        $cleanupDeadline = [DateTime]::UtcNow.AddSeconds(10)
        $remainingProcessIds = $trackedProcessIds
        while ([DateTime]::UtcNow -lt $cleanupDeadline) {
            $remainingProcessIds = @(
                $trackedProcessIds | Where-Object {
                    $null -ne (Get-Process -Id $_ -ErrorAction SilentlyContinue)
                }
            )
            if ($remainingProcessIds.Count -eq 0) {
                break
            }
            Start-Sleep -Milliseconds 200
        }
        Assert-That -Condition ($remainingProcessIds.Count -eq 0) -Message "Desktop Runtime processes remained after OpenKB.exe closed: $($remainingProcessIds -join ', ')."
    }
    finally {
        if (-not $shell.HasExited) {
            Stop-Process -Id $shell.Id -Force -ErrorAction SilentlyContinue
        }
        $shell.Dispose()
    }
}

function Assert-PackageLayout {
    param([Parameter(Mandatory = $true)] [string] $PackageRoot)

    $manifestPath = Join-Path $PackageRoot "release-manifest.json"
    foreach ($requiredFile in @(
        (Join-Path $PackageRoot "OpenKB.exe"),
        (Join-Path $PackageRoot "LICENSE"),
        (Join-Path $PackageRoot "THIRD_PARTY_NOTICES.md"),
        (Join-Path $PackageRoot "runtime\engine\OpenKBEngine.exe"),
        (Join-Path $PackageRoot "runtime\engine\_internal\rapidocr_onnxruntime\config.yaml"),
        (Join-Path $PackageRoot "runtime\engine\_internal\rapidocr_onnxruntime\models\ch_PP-OCRv4_det_infer.onnx"),
        (Join-Path $PackageRoot "runtime\engine\_internal\rapidocr_onnxruntime\models\ch_PP-OCRv4_rec_infer.onnx"),
        (Join-Path $PackageRoot "runtime\engine\_internal\rapidocr_onnxruntime\models\ch_ppocr_mobile_v2.0_cls_infer.onnx"),
        (Join-Path $PackageRoot "runtime\engine\_internal\litellm\model_prices_and_context_window_backup.json"),
        (Join-Path $PackageRoot "runtime\engine\_internal\deepdoc\det.onnx"),
        (Join-Path $PackageRoot "runtime\engine\_internal\deepdoc\rec.onnx"),
        (Join-Path $PackageRoot "runtime\engine\_internal\deepdoc\ocr.res"),
        (Join-Path $PackageRoot "runtime\engine\_internal\legacy-office\tika\tika-server-standard-3.3.2.jar"),
        (Join-Path $PackageRoot "runtime\engine\_internal\legacy-office\java\bin\java.exe"),
        (Join-Path $PackageRoot "runtime\webview2\msedgewebview2.exe"),
        $manifestPath
    )) {
        Assert-That -Condition (Test-Path -LiteralPath $requiredFile -PathType Leaf) -Message "Portable package file is missing: $requiredFile"
    }

    $shells = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -Filter "OpenKB.exe" -File)
    Assert-That -Condition ($shells.Count -eq 1) -Message "Portable package must expose exactly one OpenKB.exe."

    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    foreach ($property in @("schemaVersion", "product", "version", "platform", "entryPoint", "payloadBytes", "componentBytes", "files")) {
        Assert-That -Condition ($manifest.PSObject.Properties.Name -contains $property) -Message "Portable package manifest is missing $property."
    }
    Assert-That -Condition ($manifest.schemaVersion -eq 2) -Message "Portable package manifest has an unsupported schema."
    Assert-That -Condition ($manifest.product -eq "OpenKB") -Message "Portable package manifest has the wrong product."
    Assert-That -Condition ($manifest.platform -eq "windows-x64") -Message "Portable package manifest has the wrong platform."
    Assert-That -Condition ($manifest.version -is [string] -and -not [string]::IsNullOrWhiteSpace($manifest.version)) -Message "Portable package manifest has an invalid version."
    Assert-That -Condition ($manifest.entryPoint -eq "OpenKB.exe") -Message "Portable package manifest has the wrong entry point."
    Assert-That -Condition ($manifest.payloadBytes -is [int] -or $manifest.payloadBytes -is [int64]) -Message "Portable package manifest has an invalid payload size."
    foreach ($component in @("shell", "engine", "webView2", "deepdoc", "legacyOffice")) {
        Assert-That -Condition ($manifest.componentBytes.PSObject.Properties.Name -contains $component) -Message "Portable package manifest is missing the $component size."
        Assert-That -Condition ($manifest.componentBytes.$component -is [int] -or $manifest.componentBytes.$component -is [int64]) -Message "Portable package manifest has an invalid $component size."
    }
    $records = @($manifest.files)
    Assert-That -Condition ($records.Count -gt 0) -Message "Portable package manifest has no file records."
    [int64] $recordBytes = 0

    $packageRootPath = [System.IO.Path]::GetFullPath($PackageRoot).TrimEnd([char[]]@('\', '/'))
    $packagePrefix = "$packageRootPath$([System.IO.Path]::DirectorySeparatorChar)"
    $manifestPaths = @()
    foreach ($record in $records) {
        foreach ($property in @("path", "sha256", "bytes")) {
            Assert-That -Condition ($record.PSObject.Properties.Name -contains $property) -Message "Portable package manifest file record is missing $property."
        }
        Assert-That -Condition ($record.path -is [string] -and -not [string]::IsNullOrWhiteSpace($record.path)) -Message "Portable package manifest contains an invalid file path."
        Assert-That -Condition ($record.sha256 -is [string] -and $record.sha256 -match "^[0-9a-fA-F]{64}$") -Message "Portable package manifest contains an invalid file hash."
        Assert-That -Condition ($record.bytes -is [int] -or $record.bytes -is [int64]) -Message "Portable package manifest contains an invalid file size."
        $relativePath = $record.path
        Assert-That -Condition (-not [System.IO.Path]::IsPathRooted($relativePath)) -Message "Portable package manifest contains an absolute path."
        Assert-That -Condition (-not ($relativePath.Replace('\', '/').Split('/') -contains "..")) -Message "Portable package manifest contains a parent path."
        Assert-That -Condition (-not ($manifestPaths -contains $relativePath)) -Message "Portable package manifest contains a duplicate file path."
        $manifestPaths += $relativePath
        $filePath = [System.IO.Path]::GetFullPath((Join-Path $packageRootPath $relativePath.Replace('/', '\')))
        Assert-That -Condition ($filePath.StartsWith($packagePrefix, [System.StringComparison]::OrdinalIgnoreCase)) -Message "Portable package manifest resolves outside the package: $relativePath"
        Assert-That -Condition (Test-Path -LiteralPath $filePath -PathType Leaf) -Message "Portable package manifest references a missing file: $relativePath"
        Assert-That -Condition ((Get-Item -LiteralPath $filePath).Length -eq [int64] $record.bytes) -Message "Portable package file size changed: $relativePath"
        $actualHash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
        Assert-That -Condition ($actualHash -eq ([string] $record.sha256).ToLowerInvariant()) -Message "Portable package file hash changed: $relativePath"
        $recordBytes += [int64] $record.bytes
    }
    Assert-That -Condition ($recordBytes -eq [int64] $manifest.payloadBytes) -Message "Portable package manifest has an incorrect payload size."
    $actualComponentBytes = [ordered]@{
        shell = (Get-Item -LiteralPath (Join-Path $PackageRoot "OpenKB.exe")).Length
        engine = Get-DirectoryBytes -Path (Join-Path $PackageRoot "runtime\engine")
        webView2 = Get-DirectoryBytes -Path (Join-Path $PackageRoot "runtime\webview2")
        deepdoc = Get-DirectoryBytes -Path (Join-Path $PackageRoot "runtime\engine\_internal\deepdoc")
        legacyOffice = Get-DirectoryBytes -Path (Join-Path $PackageRoot "runtime\engine\_internal\legacy-office")
    }
    foreach ($component in $actualComponentBytes.Keys) {
        Assert-That -Condition ([int64] $manifest.componentBytes.$component -eq [int64] $actualComponentBytes[$component]) -Message "Portable package manifest has an incorrect $component size."
    }

    $inventoryPaths = @(
        Get-ChildItem -LiteralPath $packageRootPath -Recurse -File |
            ForEach-Object {
                $_.FullName.Substring($packageRootPath.Length).TrimStart([char[]]@('\', '/')).Replace('\', '/')
            } |
            Where-Object { $_ -ne "release-manifest.json" }
    )
    Assert-That -Condition ($manifestPaths.Count -eq $inventoryPaths.Count) -Message "Portable package manifest does not cover the package inventory."
    foreach ($inventoryPath in $inventoryPaths) {
        Assert-That -Condition ($manifestPaths -contains $inventoryPath) -Message "Portable package manifest is missing $inventoryPath."
    }
}

$PackageDirectory = [System.IO.Path]::GetFullPath($PackageDirectory)
Assert-That -Condition (Test-Path -LiteralPath $PackageDirectory -PathType Container) -Message "Portable package directory does not exist: $PackageDirectory"

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("OpenKB 离线 包 " + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $temporaryRoot | Out-Null
try {
    Copy-Item -LiteralPath $PackageDirectory -Destination $temporaryRoot -Recurse
    $copiedPackage = Join-Path $temporaryRoot (Split-Path -Leaf $PackageDirectory)
    Assert-PackageLayout -PackageRoot $copiedPackage
    Test-FrozenEngine -EnginePath (Join-Path $copiedPackage "runtime\engine\OpenKBEngine.exe") -WorkingDirectory $copiedPackage -ScratchDirectory $temporaryRoot
    Test-ShellProcessTree -PackageRoot $copiedPackage
    Write-Host "Portable package acceptance passed: $copiedPackage"
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        # Windows can release the disposable test lock just after the Engine exits.
        # The runner cleans any remaining temporary directory after this process.
        for ($attempt = 0; $attempt -lt 10 -and (Test-Path -LiteralPath $temporaryRoot); $attempt++) {
            try {
                Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction Stop
            }
            catch {
                Start-Sleep -Milliseconds 200
            }
        }
        if (Test-Path -LiteralPath $temporaryRoot) {
            Write-Warning "Could not remove disposable portable-package validation directory: $temporaryRoot"
        }
    }
}
